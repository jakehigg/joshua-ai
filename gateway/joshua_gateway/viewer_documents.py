"""The documents section of the viewer: the list and the page of a document.

``/documents`` lists the documents of the person who signed in and the
shared ones, one status at a time (Active, Drafts, Final, Archived), with a
chip for each kind. A row gives the title, the kind, the status, the time of
the last change, the owner, the open comments, and the summary. No other
member's document shows, and the layout has no table.

``/documents/<owner>/<slug>`` shows one document. A Markdown document shows
as blocks, with the renderer of the assignments pages (``viewer_showcase``),
and a document with the headings of a report shows as the same showcase as
the report of an assignment. Each block, card, and heading has a "+" for a
comment (``viewer_comments``). A comment names a line of the body, from 1;
the "+" of the head is line 0, the document as a whole. The ``Source`` fold
gives the frontmatter with no "+" and each line of the body with its own
"+", and the ``Versions`` fold lists the commits of the file.
``?rev=<sha>`` shows an older version, read-only.

An HTML document shows in an ``<iframe>`` with ``sandbox`` that loads
``/documents/<owner>/<slug>/artifact``: the frame has an opaque origin, so it
cannot read the viewer or its cookies, submit a form, or open a window. That
route sends ``artifact_policy``, which allows the styles of the document and
no network. Scripts stay off unless the frontmatter says ``scripts: true``;
then the sandbox and the policy allow scripts, and nothing more.

Every string from a file is untrusted and escaped. The functions here build
HTML only. The routes live in ``viewer``, and the files in
``viewer_docstore``.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from html import escape
from urllib.parse import urlencode

from joshua_gateway import viewer_docstore as ds
from joshua_gateway import viewer_report as rp
from joshua_gateway import viewer_showcase as sc
from joshua_gateway.viewer_comments import (
    CommentForms,
    Gutter,
    add_form,
    comments_badge,
    time_html,
)

# The pills of the list, in order: (status, label, the text when empty).
PILLS = (
    ("active", "Active", "No active document."),
    ("draft", "Drafts", "No draft."),
    ("final", "Final", "No final document."),
    ("archived", "Archived", "No archived document."),
)
DEFAULT_STATUS = "active"
_PILL_KEYS = frozenset(key for key, _, _ in PILLS)

_STATUS_CLASS = {"draft": "plain", "active": "accent", "final": "ok", "archived": "plain"}

# The sandbox of an HTML document. Never ``allow-same-origin``: with it, a
# script in the frame could reach the viewer.
SANDBOX = ""
SANDBOX_SCRIPTS = "allow-scripts"


def artifact_policy(scripts: bool) -> str:
    """The content policy of ``/documents/<owner>/<slug>/artifact``.

    ``default-src 'none'`` blocks every load, so the document reaches no
    network: no fetch, no font, and no stylesheet of its own. Inline styles
    are allowed, so a ``<style>`` element and a ``style`` attribute work.
    Images come from the viewer only. ``form-action`` and ``frame-src``
    block a form and a frame of the document. The ``sandbox`` directive adds
    the sandbox of the frame to the response itself, never with
    ``allow-same-origin``, so the document has an opaque origin also when a
    browser opens the address alone. Scripts run only with ``scripts: true``,
    and only from the document.
    """
    script_src = "script-src 'unsafe-inline'; " if scripts else ""
    sandbox = "sandbox allow-scripts" if scripts else "sandbox"
    return (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        f"{script_src}form-action 'none'; frame-src 'none'; "
        f"frame-ancestors 'self'; {sandbox}"
    )


def parse_status(raw: str | None) -> str:
    return raw if raw in _PILL_KEYS else DEFAULT_STATUS


def parse_kind(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value if value and len(value) <= 40 and value.replace("-", "").isalnum() else None


def status_chip(status: str) -> str:
    css = _STATUS_CLASS.get(status, "plain")
    return f'<span class="j-badge {css}">{escape(status)}</span>'


def kind_chip(kind: str) -> str:
    return f'<span class="a-tag d-kind">{escape(kind)}</span>'


def owner_label(owner: str, person_id: str) -> str:
    return "yours" if owner == person_id else "shared"


# -- the list ---------------------------------------------------------------


def _href(status: str, kind: str | None) -> str:
    query = {"status": status} | ({"kind": kind} if kind else {})
    return "/documents?" + urlencode(query)


def _pills(status: str, kind: str | None) -> str:
    links = []
    for key, label, _ in PILLS:
        if key == status:
            links.append(f'<span class="j-chip on" aria-current="true">{label}</span>')
        else:
            links.append(f'<a class="j-chip" href="{escape(_href(key, kind))}">{label}</a>')
    return f'<div class="j-chips" role="group" aria-label="Status">{"".join(links)}</div>'


def _kind_chips(status: str, kind: str | None, present: list[str]) -> str:
    kinds = list(dict.fromkeys([*ds.KINDS, *present]))
    links = []
    for key, label in [(None, "all kinds"), *[(k, k) for k in kinds]]:
        if key == kind:
            links.append(f'<span class="j-chip sm on" aria-current="true">{escape(label)}</span>')
        else:
            href = _href(status, key)
            links.append(f'<a class="j-chip sm" href="{escape(href)}">{escape(label)}</a>')
    return f'<div class="j-chips d-kinds" role="group" aria-label="Kind">{"".join(links)}</div>'


def row_html(doc: ds.Document, *, person_id: str, comments: int, tz: tzinfo, now: datetime) -> str:
    """One row: the title as a link, the summary, the kind, the owner, the
    time of the last change, the open comments, and the status."""
    updated = time_html(doc.updated, tz=tz, now=now)
    sub = " · ".join(
        part
        for part in (
            kind_chip(doc.kind),
            escape(owner_label(doc.owner, person_id)),
            f"updated {updated}" if updated else "",
            "html" if doc.format == "html" else "",
        )
        if part
    )
    summary = f'<span class="a-row-line">{escape(doc.summary)}</span>' if doc.summary else ""
    return (
        '<li class="j-list-row a-row d-row"><div class="j-list-main">'
        f'<a class="j-list-title wrap" href="{escape(doc.url)}">{escape(doc.title)}</a>'
        f'{summary}<span class="j-list-sub">{sub}</span></div>'
        f'<div class="a-chips">{comments_badge(comments)}{status_chip(doc.status)}</div></li>'
    )


def list_html(
    docs: list[ds.Document] | None,
    *,
    status: str,
    kind: str | None,
    person_id: str,
    comments: dict[str, int],
    tz: tzinfo,
    now: datetime,
) -> str:
    """The body of ``/documents``. ``docs`` None means there is no documents
    folder yet. ``comments`` maps the path of a document to its open comments."""
    head = '<h1 class="j-doc-title">documents</h1>'
    present = sorted({d.kind for d in docs or []})
    bar = (
        f'<div class="a-bar d-bar">{_pills(status, kind)}</div>{_kind_chips(status, kind, present)}'
    )
    if docs is None:
        return (
            f"{head}{bar}"
            '<p class="j-empty">No documents yet. Joshua keeps a plan, a report, or a spec '
            "here when you ask for one.</p>"
        )
    shown = [d for d in docs if d.status == status and (kind is None or d.kind == kind)]
    if not shown:
        empty = next(text for key, _, text in PILLS if key == status)
        if kind:
            empty = f"{empty[:-1]} of the kind {kind}."
        return f'{head}{bar}<p class="j-empty">{escape(empty)}</p>'
    rows = "".join(
        row_html(d, person_id=person_id, comments=comments.get(d.tool_path, 0), tz=tz, now=now)
        for d in shown
    )
    return f'{head}{bar}<ul class="j-list a-list d-list">{rows}</ul>'


# -- one document -----------------------------------------------------------


def _crumbs(doc: ds.Document, person_id: str) -> str:
    return (
        '<nav class="j-crumbs"><a href="/documents">documents</a>'
        f' <span aria-hidden="true">/</span> {escape(owner_label(doc.owner, person_id))}</nav>'
    )


def _links(doc: ds.Document) -> str:
    parts = []
    if doc.assignment.isdigit():
        parts.append(
            f'<a class="j-chip a-saved" href="/assignments/{int(doc.assignment)}">'
            f"assignment {int(doc.assignment)}</a>"
        )
    promoted = doc.promoted_to
    if promoted.startswith("wiki/") and ".." not in promoted:
        parts.append(
            f'<a class="j-chip a-saved" href="/{escape(promoted)}">promoted to the wiki</a>'
        )
    return "".join(parts)


def _header(
    doc: ds.Document,
    *,
    person_id: str,
    open_comments: int,
    title: bool,
    lede: str,
    head_last: int,
    gutter: Gutter | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    """The head of the page: the chips, the title, the time, and the lede.
    With a gutter, the head is the unit of line 0 (the document as a whole)
    and of the body lines up to ``head_last`` (the title and the lede of a
    report). Its "+" is on line 0, anchored to the ``title:`` line."""
    updated = time_html(doc.updated, tz=tz, now=now)
    meta = f'<span class="j-meta">updated {updated}</span>' if updated else ""
    chips = (
        f"{status_chip(doc.status)}{kind_chip(doc.kind)}{_links(doc)}"
        f"{comments_badge(open_comments)}{meta}"
    )
    heading = f'<h1 class="j-doc-title">{escape(doc.title)}</h1>' if title else ""
    lede_html = f'<p class="a-lede">{sc.inline(lede)}</p>' if lede else ""
    anchors = plus = cards = anchor = ""
    css = "a-hero d-hero"
    if gutter is not None and (doc.offset or head_last):
        first = 0
        last = max(first, head_last)
        anchors, cards = gutter.anchors(first, last), gutter.take(first, last)
        if gutter.forms is not None:
            plus = add_form(0, doc.title_line, gutter.forms, what="the document")
        css += " d-unit" + (" has-comments" if cards else "")
        anchor = f' id="{gutter.unit_id(first)}"'
    return (
        f'<header class="{css}"{anchor}>{anchors}<div class="a-chips">{chips}</div>'
        f"{heading}{lede_html}{plus}{cards}</header>"
    )


def _versions(doc: ds.Document, versions: list[ds.Version], current: str | None) -> str:
    """The commits of the file, folded. Each one but the newest links to its
    read-only page."""
    if not versions:
        return sc.fold("Versions", '<p class="j-faint">No version history.</p>')
    rows = []
    for n, version in enumerate(versions):
        when = version.when[:16].replace("T", " ")
        label = f"{escape(when)} · <code>{escape(version.sha[:8])}</code> {escape(version.subject)}"
        if n == 0:
            href = doc.url
            label += ' <span class="j-faint">(now)</span>'
        else:
            href = f"{doc.url}?{urlencode({'rev': version.sha})}"
        mark = ' aria-current="true"' if version.sha == current else ""
        rows.append(f'<li><a href="{escape(href)}"{mark}>{label}</a></li>')
    hint = f'<span class="a-fold-hint">{sc.count(len(versions), "version")}</span>'
    return sc.fold("Versions", f'<ol class="d-versions">{"".join(rows)}</ol>', hint=hint)


def _frame(doc: ds.Document, rev: str = "") -> str:
    """The document in a sandboxed frame of the artifact route. ``rev`` is the
    commit of an older version, else ""."""
    sandbox = SANDBOX_SCRIPTS if doc.scripts else SANDBOX
    src = f"{doc.url}/artifact" + (f"?{urlencode({'rev': rev})}" if rev else "")
    note = (
        "Scripts of this document run in a sandbox, apart from the viewer."
        if doc.scripts
        else "Scripts of this document do not run."
    )
    return (
        '<section class="d-artifact">'
        f'<iframe class="d-frame" sandbox="{sandbox}" src="{escape(src)}" '
        f'title="{escape(doc.title)}" referrerpolicy="no-referrer" loading="lazy"></iframe>'
        f'<p class="j-faint d-frame-note">{escape(note)}</p></section>'
    )


def _showcase(doc: ds.Document, report: rp.Report, gutter: Gutter) -> tuple[str, str, int]:
    """A document with the headings of a report: the lede, the sections, and
    the last line of the title and the lede."""
    cards = sc.output_cards([], report)
    decisions = len(sc.decision_items(report.get(rp.DECISIONS)))
    numbers = []
    if decisions:
        numbers.append(sc.tile(str(decisions), "decisions" if decisions != 1 else "decision"))
    if cards:
        numbers.append(sc.tile(str(len(cards)), "outputs" if len(cards) != 1 else "output"))
    lede_last = (report.preamble_line or report.first_section) - 1
    parts = [
        sc.tiles(numbers, label="Numbers") if numbers else "",
        sc.highlights_html(report.get(rp.HIGHLIGHTS), gutter),
        sc.decisions_html(f"Document {doc.tool_path}", report.get(rp.DECISIONS), gutter),
        sc.outputs_html(cards, part=report.get(rp.OUTPUTS), gutter=gutter),
        sc.asked_html(report.get(rp.ASKED), gutter),
        sc.done_html(report, gutter),
        sc.follow_ups_html(report.get(rp.FOLLOW_UPS), gutter),
        sc.prose_section_html("Cost", report.get(rp.COST), gutter),
        sc.COPY_SCRIPT_TAG if decisions else "",
    ]
    return report.lede, "".join(p for p in parts if p), max(0, lede_last)


def document_html(
    doc: ds.Document,
    *,
    person_id: str,
    comments: list[ds.DocComment],
    forms: CommentForms | None,
    versions: list[ds.Version],
    version: ds.Version | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    """The body of ``/documents/<owner>/<slug>``. ``version`` set, the page
    is that older version: read-only, with no comment."""
    if version is not None:
        comments, forms = [], None
    gutter = Gutter(
        doc.body_lines,
        comments,
        forms,
        tz,
        now,
        author=person_id,
        preamble=doc.lines[: doc.offset],
    )
    open_comments = len(gutter.open)
    lede = ""
    head_last = 0
    title = True
    if doc.format == "html":
        body = _frame(doc, version.sha if version is not None else "")
    else:
        report = rp.parse(doc.body)
        if report.structured:
            lede, body, head_last = _showcase(doc, report, gutter)
        else:
            blocks = sc.blocks_html(doc.body, gutter=gutter)
            # A body that starts with its own title gives the title once.
            title = not doc.body.lstrip().startswith("# ")
            body = f'<article class="j-prose a-blocks d-body">{blocks}</article>'
    head = _header(
        doc,
        person_id=person_id,
        open_comments=open_comments,
        title=title,
        lede=lede,
        head_last=head_last,
        gutter=gutter,
        tz=tz,
        now=now,
    )
    old = ""
    if version is not None:
        when = version.when[:16].replace("T", " ")
        old = (
            '<aside class="a-notice d-old" role="note">'
            f"<strong>The version of {escape(when)}</strong>"
            f'<p class="a-text">This is an older version, <code>{escape(version.sha[:8])}</code>. '
            f'It is read-only. <a href="{escape(doc.url)}">Read the current version</a>.</p>'
            "</aside>"
        )
    folds = (
        f'<div class="a-folds">{gutter.orphans()}{gutter.source()}{gutter.resolved()}'
        f"{_versions(doc, versions, version.sha if version else None)}</div>"
    )
    return f"{_crumbs(doc, person_id)}{head}{old}{body}{folds}"


def message_html(title: str, body: str) -> str:
    """A page body with a title and a message. ``body`` is HTML."""
    return (
        '<nav class="j-crumbs"><a href="/documents">documents</a></nav>'
        f'<h1 class="j-doc-title">{escape(title)}</h1>{body}'
    )
