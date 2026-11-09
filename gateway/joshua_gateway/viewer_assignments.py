"""The assignments section of the viewer: calls to the manager, and their HTML.

The assignments container keeps each assignment of a person: the
definition, the state, the plan, the progress, and at the end the report. The
viewer asks the read API of the manager (``GET /v1/assignments``,
``GET /v1/assignments/{id}``, ``GET /v1/saved``, ``GET /v1/saved/{id}``) with
its own fleet token, and names the person who signed in as ``person``. The
manager shows a person only their own assignments.

A person reads an assignment here and tells Joshua what to change. The one
write is a comment on a line of a definition: the definition shows each line
with its number, and the "+" of a line opens a plain form. The viewer sends
the comment to the manager as the person who signed in
(``POST /v1/assignments/{id}/comments`` or ``POST /v1/saved/{id}/comments``),
and a Delete button of an open comment deletes it. Joshua reads the comments,
changes the spec, and resolves them. The line numbers count the lines of the
``definition`` text of the manager, so the viewer shows that text as it is.

The viewer needs ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either
one missing, :func:`from_env` returns None and the pages say so. A call that
fails returns None, and the page says that the manager did not answer and
shows nothing old. A ``404`` returns :data:`NOT_FOUND`. Each page answers 200
in these cases, except a detail page that the manager does not know, which is
404.

Every string from the manager is untrusted, and the HTML escapes each one. The
plan and the report are Markdown, rendered as a wiki page is, with raw HTML
off. A reference of an output is a link only when it is an ``http`` or
``https`` URL with a host. The layout has no table: the texts are long, so a
detail page is a document.

A completed assignment with a report shows the report as the page.
``viewer_report`` cuts the report at its fixed headings, and each section
gets its own element: a hero with the lede and a stat row, the highlights as
cards, the decisions as numbered cards, the outputs as cards by kind, what
was asked, what was done, the follow-ups, and the cost. The plan, the
progress, and the definition fold under it. A report with none of the
headings shows as Markdown. Any other assignment shows its plan, the bar of
its task boxes, the next step, the last checkpoint, and the outputs so far.
A page of an assignment that is in progress refreshes itself every 20
seconds with a ``<meta>`` element.

One small script, :data:`COPY_SCRIPT`, shows the copy button of each
decision. The policy of the viewer allows that script by its hash and no
other. With no script, the buttons stay hidden and the page is the same.

The functions here build data and HTML only. The routes live in ``viewer``.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from html import escape
from typing import Any, Final
from urllib.parse import urlencode, urlparse

import httpx
import yaml
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_gateway import viewer_report as rp
from joshua_gateway.viewer_limits import when_text
from joshua_gateway.viewer_usage import exact, short

logger = get_logger("viewer.assignments")

ASSIGNMENTS_URL_ENV = "ASSIGNMENTS_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
LIST_PATH = "/v1/assignments"
SAVED_PATH = "/v1/saved"

# A person waits for this page, so a slow manager must not hold it open.
TIMEOUT_S = 5.0

# The most rows that the list page asks for.
LIST_LIMIT = 100

# The seconds between two loads of a page of an assignment in progress.
REFRESH_S = 20

# The states as a person sees them, in the order of the pills.
STATES = (("in_progress", "In progress"), ("queued", "Queued"), ("completed", "Completed"))
_STATE_KEYS = frozenset(key for key, _ in STATES)

# The badge class of each status of the store. An unknown status is plain.
_STATUS_CLASS = {
    "queued": "plain",
    "running": "accent",
    "waiting": "warn",
    "paused_limits": "warn",
    "done": "ok",
    "failed": "bad",
    "cancelled": "plain",
    "budget_exceeded": "bad",
    "time_exceeded": "bad",
}
_STATUS_LABEL = {
    "paused_limits": "paused at limits",
    "budget_exceeded": "over budget",
    "time_exceeded": "over time",
}

# The limit windows, as the usage page names them.
_WINDOW_LABEL = {"five_hour": "5-hour", "seven_day": "weekly"}

# The wiki renderer: raw HTML is off, so a ``<script>`` in a plan renders as
# text, and markdown-it refuses a ``javascript:`` link.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])


# -- the contract of the read API -------------------------------------------


class _Model(BaseModel):
    # A newer manager can add a field, so an unknown field is ignored.
    model_config = ConfigDict(extra="ignore")


class Usage(_Model):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0


class SavedRef(_Model):
    id: int
    name: str


class Item(_Model):
    id: int
    title: str
    status: str
    state: str
    model: str = ""
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    working_s: int = 0
    # None from a manager that does not give it.
    turns: int | None = None
    usage: Usage = Field(default_factory=Usage)
    outputs_count: int = 0
    lede: str | None = None
    next_step: str | None = None
    open_question: str | None = None
    asked_at: str | None = None
    stopped_at_limits: dict[str, Any] | None = None
    saved: SavedRef | None = None


class Output(_Model):
    kind: str = ""
    ref: str = ""
    title: str = ""
    at: str | None = None


class Checkpoint(_Model):
    at: str | None = None
    note: str = ""


class Comment(_Model):
    id: int
    target: str = "definition"
    line_no: int
    line_text: str = ""
    body: str = ""
    created_at: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None
    stale: bool = False


class Detail(Item):
    spec: dict[str, Any] = Field(default_factory=dict)
    definition: str | None = None
    comments: list[Comment] = Field(default_factory=list)
    open_comments: int = 0
    plan: str | None = None
    plan_at: str | None = None
    report: str | None = None
    report_at: str | None = None
    outputs: list[Output] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    summary: str | None = None
    follow_ups: list[Any] | None = None
    answer: str | None = None
    error: str | None = None


class ItemList(_Model):
    items: list[Item]


class LastRun(_Model):
    id: int
    status: str
    finished_at: str | None = None


class SavedItem(_Model):
    id: int
    name: str
    title: str = ""
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    runs: int = 0
    last_run: LastRun | None = None
    open_comments: int = 0


class SavedList(_Model):
    items: list[SavedItem]


class SavedDetail(SavedItem):
    runs_list: list[Item] = Field(default_factory=list)
    definition: str | None = None
    comments: list[Comment] = Field(default_factory=list)


# -- the client -------------------------------------------------------------


class NotFound:
    """The manager answered 404: no such row for this person."""

    def __repr__(self) -> str:
        return "NOT_FOUND"


NOT_FOUND: Final = NotFound()

# What a comment belongs to, and the path of its parent in the read API.
KINDS = {"assignment": LIST_PATH, "saved": SAVED_PATH}


@dataclass(frozen=True)
class Written:
    """The answer of the manager to a comment write: the status, and its error text."""

    status: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (201, 204)


class AssignmentsClient:
    """Read the assignments of one person from the manager."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def _get(
        self, path: str, params: dict[str, Any], client: FleetClient | None = None
    ) -> Any:
        """The JSON body, :data:`NOT_FOUND` on a 404, or None on a failure."""
        try:
            if client is not None:
                response = await client.get_json(path, params=params)
            else:
                async with self._client() as own:
                    response = await own.get_json(path, params=params)
        except httpx.HTTPError as exc:
            logger.warning({"message": "assignments read failed", "error": type(exc).__name__})
            return None
        if response.status_code == 404:
            return NOT_FOUND
        if response.status_code != 200:
            logger.warning({"message": "assignments read refused", "status": response.status_code})
            return None
        try:
            return response.json()
        except ValueError:
            logger.warning({"message": "assignments read answered an unknown shape"})
            return None

    def _client(self) -> FleetClient:
        return FleetClient(self._base_url, self._token, TIMEOUT_S, transport=self._transport)

    async def _read[M: BaseModel](
        self,
        model: type[M],
        path: str,
        params: dict[str, Any],
        client: FleetClient | None = None,
    ) -> M | NotFound | None:
        body = await self._get(path, params, client)
        if body is None or body is NOT_FOUND:
            return body
        try:
            return model.model_validate(body)
        except ValidationError:
            logger.warning({"message": "assignments read answered an unknown shape"})
            return None

    async def list(self, person: str, state: str) -> ItemList | None:
        """The assignments of ``person`` in ``state``, newest first."""
        result = await self._read(
            ItemList, LIST_PATH, {"person": person, "state": state, "limit": LIST_LIMIT}
        )
        return None if isinstance(result, NotFound) else result

    async def detail(self, person: str, assignment_id: int) -> Detail | NotFound | None:
        """One assignment of ``person``."""
        return await self._read(Detail, f"{LIST_PATH}/{assignment_id}", {"person": person})

    async def _write(self, method: str, path: str, person: str, body: Any = None) -> Written | None:
        """Send one comment write. None when the manager did not answer."""
        params = {"person": person}
        try:
            async with self._client() as client:
                if method == "POST":
                    response = await client.post_json(path, json=body, params=params)
                else:
                    response = await client.delete(path, params=params)
        except httpx.HTTPError as exc:
            logger.warning({"message": "comment write failed", "error": type(exc).__name__})
            return None
        error = ""
        if response.status_code not in (201, 204):
            logger.warning({"message": "comment write refused", "status": response.status_code})
            try:
                error = str(response.json().get("error") or "")
            except (ValueError, AttributeError):
                error = ""
        return Written(response.status_code, error[:300])

    async def add_comment(
        self, person: str, kind: str, parent_id: int, *, line_no: int, line_text: str, body: str
    ) -> Written | None:
        """Write a comment of ``person`` on line ``line_no`` of a definition."""
        path = f"{KINDS[kind]}/{parent_id}/comments"
        fields = {"line_no": line_no, "line_text": line_text, "body": body}
        return await self._write("POST", path, person, fields)

    async def delete_comment(
        self, person: str, kind: str, parent_id: int, comment_id: int
    ) -> Written | None:
        """Delete an open comment of ``person``."""
        return await self._write(
            "DELETE", f"{KINDS[kind]}/{parent_id}/comments/{comment_id}", person
        )

    async def saved(self, person: str) -> SavedList | None:
        """The saved assignments of ``person``."""
        result = await self._read(SavedList, SAVED_PATH, {"person": person})
        return None if isinstance(result, NotFound) else result

    async def saved_detail(self, person: str, saved_id: int) -> SavedDetail | NotFound | None:
        """One saved assignment of ``person``, with its runs."""
        return await self._read(SavedDetail, f"{SAVED_PATH}/{saved_id}", {"person": person})


def from_env(env: Mapping[str, str]) -> AssignmentsClient | None:
    """A client from ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None."""
    base_url = env.get(ASSIGNMENTS_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return AssignmentsClient(base_url, token)


def parse_state(raw: str | None) -> str | None:
    """The ``state`` query value, or None when it is not a state."""
    return raw if raw in _STATE_KEYS else None


# -- small parts ------------------------------------------------------------


def _moment(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


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
    moment = _moment(text)
    if moment is None:
        return ""
    local = moment.astimezone(tz)
    full = f"{local:%Y-%m-%d %H:%M} {local.tzname() or ''}".strip()
    label = ago((now - moment).total_seconds())
    return (
        f'<time datetime="{escape(moment.isoformat())}" title="{escape(full)}">'
        f"{escape(label)}</time>"
    )


def duration(seconds: int) -> str:
    """A working time: ``45s``, ``12m``, ``1h 20m``."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def status_chip(status: str) -> str:
    """The badge of a status of the store."""
    css = _STATUS_CLASS.get(status, "plain")
    label = _STATUS_LABEL.get(status, status.replace("_", " "))
    return f'<span class="j-badge {css}">{escape(label)}</span>'


def is_url(ref: str) -> bool:
    """True for an ``http`` or ``https`` URL with a host."""
    try:
        parsed = urlparse(ref.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def markdown(text: str) -> str:
    """Markdown as the wiki renders it."""
    return _MD.render(text)


def _when(item: Item, *, tz: tzinfo, now: datetime) -> str:
    if item.started_at:
        return f"started {time_html(item.started_at, tz=tz, now=now)}"
    if item.created_at:
        return f"created {time_html(item.created_at, tz=tz, now=now)}"
    return ""


def _sub(parts: list[str]) -> str:
    return " · ".join(part for part in parts if part)


def _not_configured() -> str:
    return (
        '<p class="j-empty">Assignments are not configured. The viewer needs '
        f"<code>{ASSIGNMENTS_URL_ENV}</code> and <code>{VIEWER_TOKEN_ENV}</code>.</p>"
    )


def _no_answer() -> str:
    return (
        '<p class="j-empty">The assignments manager did not answer. '
        "The assignments are not available now.</p>"
    )


def row_line(item: Item) -> str:
    """The line under the title of a row, as Markdown: the lede of the report,
    else the next step of the plan, else "". The manager gives both."""
    if item.lede and item.lede.strip():
        return item.lede.strip()
    step = (item.next_step or "").strip()
    return f"Next: {step}" if step else ""


def item_row(item: Item, *, tz: tzinfo, now: datetime) -> str:
    """One row: the title as a link, the lede of the report or the next step
    of the plan, the status, when, the model, the tokens, and the number of
    outputs."""
    question = (
        '<span class="j-badge warn plain" title="The assignment asks a question">question</span>'
        if item.open_question
        else ""
    )
    line = row_line(item)
    outputs = item.outputs_count
    sub = _sub(
        [
            _when(item, tz=tz, now=now),
            escape(item.model),
            f'<span title="{exact(item.usage.total)} tokens">'
            f"{short(item.usage.total)} tokens</span>",
            _count(outputs, "output") if outputs else "",
        ]
    )
    lede = f'<span class="a-row-line">{_inline(line)}</span>' if line else ""
    return (
        '<li class="j-list-row a-row"><div class="j-list-main">'
        f'<a class="j-list-title wrap" href="/assignments/{item.id}">{escape(item.title)}</a>'
        f'{lede}<span class="j-list-sub">{sub}</span></div>'
        f'<div class="a-chips">{question}{status_chip(item.status)}</div></li>'
    )


def _rows(items: list[Item], *, tz: tzinfo, now: datetime) -> str:
    rows = "".join(item_row(i, tz=tz, now=now) for i in items)
    return f'<ul class="j-list a-list">{rows}</ul>'


def _pills(current: str | None) -> str:
    """The pills of the states. ``current`` None selects none."""
    links = []
    for key, label in STATES:
        if key == current:
            links.append(f'<span class="j-chip on" aria-current="true">{label}</span>')
        else:
            href = "/assignments?" + urlencode({"state": key})
            links.append(f'<a class="j-chip" href="{escape(href)}">{label}</a>')
    return (
        '<div class="a-bar">'
        f'<div class="j-chips" role="group" aria-label="State">{"".join(links)}</div>'
        '<a class="a-saved-link" href="/assignments/saved">Saved</a></div>'
    )


# -- the pages --------------------------------------------------------------


def list_html(
    body: ItemList | None,
    *,
    state: str | None,
    configured: bool,
    tz: tzinfo,
    now: datetime,
) -> str:
    """The body of ``/assignments``. ``body`` None means the manager did not
    answer. ``state`` None selects no pill."""
    head = f'<h1 class="j-doc-title">Assignments</h1>{_pills(state)}'
    if not configured:
        return head + _not_configured()
    if body is None:
        return head + _no_answer()
    if not body.items:
        label = dict(STATES).get(state or "", "here").lower()
        return f'{head}<p class="j-empty">No assignment is {escape(label)}.</p>'
    return head + _rows(body.items, tz=tz, now=now)


def _limits_text(stop: Mapping[str, Any], *, tz: tzinfo, now: datetime) -> str:
    """The limits stop as one plain sentence or two. Not escaped."""
    window = stop.get("window")
    if isinstance(window, str) and window:
        name = _WINDOW_LABEL.get(window, window.replace("_", " "))
        text = f"Stopped at the {name} limit"
        used, ceiling = stop.get("used_pct"), stop.get("ceiling")
        if isinstance(used, int | float) and isinstance(ceiling, int | float):
            text += f": {used:g}% used, the ceiling is {ceiling:g}%"
        text += "."
        resets = when_text(stop.get("resets_at"), tz=tz, now=now)
        if resets:
            text += f" The window resets {resets}."
    else:
        text = "Stopped because the subscription limits are not known."
    step = stop.get("next_step")
    if isinstance(step, str) and step.strip():
        text += f" Next step: {step.strip()}"
    return text


def _notice(item: Detail, *, tz: tzinfo, now: datetime) -> str:
    if item.open_question:
        asked = time_html(item.asked_at, tz=tz, now=now)
        when = f" {asked}" if asked else ""
        return (
            '<aside class="a-notice warn" role="note">'
            f"<strong>The assignment asks{when}</strong>"
            f'<p class="a-text">{escape(item.open_question)}</p>'
            '<p class="j-muted">Answer it in a conversation with Joshua.</p></aside>'
        )
    if item.stopped_at_limits:
        return (
            '<aside class="a-notice warn" role="note">'
            "<strong>Paused at the subscription limits</strong>"
            f'<p class="a-text">{escape(_limits_text(item.stopped_at_limits, tz=tz, now=now))}'
            "</p></aside>"
        )
    return ""


def _section(
    title: str, body: str, *, stamp: str = "", count: str = "", css: str = "a-section"
) -> str:
    when = f'<span class="j-meta">updated {stamp}</span>' if stamp else ""
    badge = f'<span class="a-count">{escape(count)}</span>' if count else ""
    return (
        f'<section class="{css}"><div class="a-head"><h2>{escape(title)}</h2>{badge}{when}</div>'
        f"{body}</section>"
    )


def _prose(text: str | None, empty: str) -> str:
    if text and text.strip():
        return f'<article class="j-prose">{markdown(text)}</article>'
    return f'<p class="j-faint">{escape(empty)}</p>'


# The icon of each kind of output: name -> SVG path data on a 24px grid. A
# kind matches the first name whose words it holds; any other kind is
# "other".
_KIND_ICONS: dict[str, str] = {
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
    """The icon name of a kind of output: a fixed name of ``_KIND_ICONS``.

    A long word matches anywhere in the kind; ``mr`` and ``pr`` match only as
    a whole word, so "summary" is not a merge request."""
    text = kind.lower()
    words = set(_SHORT_WORD.findall(text))
    for name, needles in _KIND_WORDS:
        for needle in needles:
            if (needle in words) if len(needle) <= 2 else (needle in text):
                return name
    return "other"


def _svg(paths: str, css: str = "j-icon") -> str:
    return f'<svg class="{css}" viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'


def _inline(text: str) -> str:
    """Inline Markdown, escaped, as the wiki renders it."""
    return _MD.renderInline(text)


def _ref_key(ref: str) -> str:
    return ref.strip().strip("<>").rstrip("/")


@dataclass(frozen=True)
class Card:
    """One output card: the recorded output and the note from the report."""

    kind: str
    ref: str
    title: str
    note: str = ""


def output_cards(outputs: list[Output], report: rp.Report | None) -> list[Card]:
    """The output cards. The recorded outputs are the cards; a line of the
    Outputs section of the report gives the note of the card with the same
    ``ref``. With no recorded output, the lines of the section are the cards."""
    section = report.get(rp.OUTPUTS) if report is not None else None
    lines = rp.output_lines(section.body) if section is not None else []
    if outputs:
        notes = {_ref_key(line.ref): line.note for line in lines if line.ref and line.note}
        return [Card(o.kind, o.ref, o.title, notes.get(_ref_key(o.ref), "")) for o in outputs]
    return [Card(line.kind, line.ref, line.title, line.note) for line in lines]


def output_card(card: Card) -> str:
    """One output: the icon of its kind, the title (a link when ``ref`` is an
    ``http`` or ``https`` URL), any other ``ref`` as code, and the note."""
    icon = _svg(_KIND_ICONS[kind_icon(card.kind)], "j-icon a-out-icon")
    ref = card.ref.strip()
    title = card.title or ref
    if ref and is_url(ref):
        host = urlparse(ref).netloc
        head = (
            f'<a class="a-out-title" href="{escape(ref)}" rel="noreferrer">{escape(title)}</a>'
            f'<span class="a-out-ref">{escape(host)}</span>'
        )
    elif card.title:
        code = f'<code class="a-out-ref">{escape(ref)}</code>' if ref else ""
        head = f'<span class="a-out-title">{escape(card.title)}</span>{code}'
    elif ref:
        head = f'<code class="a-out-title">{escape(ref)}</code>'
    else:
        head = ""
    note = f'<p class="a-out-note">{_inline(card.note)}</p>' if card.note else ""
    return f'<li class="a-out">{icon}<div class="a-out-main">{head}{note}</div></li>'


def _outputs(cards: list[Card], *, title: str = "Outputs") -> str:
    """The output cards grouped by kind, in the order each kind first comes."""
    if not cards:
        return ""
    groups: dict[str, list[Card]] = {}
    for card in cards:
        groups.setdefault(card.kind.strip().lower(), []).append(card)
    body = "".join(
        '<div class="a-group">'
        f'<h3 class="j-label">{escape(group[0].kind.strip() or "other")}'
        f' <span class="j-count">{len(group)}</span></h3>'
        f'<ul class="a-cards a-outs">{"".join(output_card(c) for c in group)}</ul></div>'
        for group in groups.values()
    )
    return _section(title, body, count=_count(len(cards), "output"))


def _progress(checkpoints: list[Checkpoint], *, tz: tzinfo, now: datetime) -> str:
    if not checkpoints:
        return '<p class="j-faint">No checkpoint yet.</p>'
    items = "".join(
        f'<li><span class="a-at">{time_html(point.at, tz=tz, now=now)}</span>'
        f'<p class="a-text">{escape(point.note)}</p></li>'
        for point in checkpoints
    )
    return f'<ol class="a-timeline">{items}</ol>'


def spec_yaml(spec: Mapping[str, Any]) -> str:
    """The definition as YAML, in the order of the manager. Not escaped."""
    text = yaml.safe_dump(dict(spec), sort_keys=False, allow_unicode=True, width=100)
    return text.strip()


# The longest comment, as the manager takes it.
MAX_COMMENT = 2000


@dataclass(frozen=True)
class CommentForms:
    """Where the comment forms of a definition post: ``action`` is the path of
    the comments of the parent in the viewer, ``csrf`` the token of the forms."""

    action: str
    csrf: str


def comments_badge(n: int) -> str:
    """The count of open comments: "3 comments for Joshua", or "" for none."""
    if n <= 0:
        return ""
    text = f"{n} comment{'' if n == 1 else 's'} for Joshua"
    return f'<span class="j-badge accent a-comments">{escape(text)}</span>'


def _csrf_input(forms: CommentForms) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(forms.csrf)}">'


def _comment_card(
    comment: Comment, *, forms: CommentForms | None, tz: tzinfo, now: datetime
) -> str:
    """One open comment: the body, the age, a stale mark, and Delete."""
    age = time_html(comment.created_at, tz=tz, now=now)
    stale = ""
    was = ""
    if comment.stale:
        stale = '<span class="j-badge warn">line changed</span>'
        was = (
            '<p class="a-was"><span class="j-faint">It was on:</span> '
            f"<code>{escape(comment.line_text)}</code></p>"
        )
    delete = ""
    if forms is not None:
        action = f"{forms.action}/{comment.id}/delete"
        delete = (
            f'<form class="a-del" method="post" action="{escape(action)}">'
            f'{_csrf_input(forms)}<button class="j-btn sm" type="submit">Delete</button></form>'
        )
    return (
        f'<div class="a-comment{" stale" if comment.stale else ""}" id="C{comment.id}">'
        f'<p class="a-comment-body">{escape(comment.body)}</p>{was}'
        f'<div class="a-comment-foot"><span class="j-faint">{age}</span>{stale}{delete}</div></div>'
    )


def _add_form(n: int, text: str, forms: CommentForms) -> str:
    """The "+" of a line: a closed ``<details>`` with the form of a new comment."""
    return (
        f'<details class="a-add"><summary class="a-plus" title="Comment on line {n}" '
        f'aria-label="Comment on line {n}">+</summary>'
        f'<form class="a-comment-form" method="post" action="{escape(forms.action)}">'
        f'{_csrf_input(forms)}<input type="hidden" name="line_no" value="{n}">'
        f'<input type="hidden" name="line_text" value="{escape(text)}">'
        f'<div class="j-field"><textarea name="body" rows="3" maxlength="{MAX_COMMENT}" '
        f'required aria-label="Your comment on line {n}" '
        'placeholder="Tell Joshua what to change"></textarea></div>'
        '<button class="j-btn sm" type="submit">Comment</button></form></details>'
    )


def _resolved(comments: list[Comment], *, tz: tzinfo, now: datetime) -> str:
    """The resolved comments, folded, each with the resolution of Joshua."""
    if not comments:
        return ""
    rows = "".join(
        f'<li class="a-comment resolved" id="C{c.id}">'
        f'<p class="j-faint">Line {c.line_no}: <code>{escape(c.line_text)}</code></p>'
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


def definition_html(
    text: str | None,
    spec: Mapping[str, Any],
    comments: list[Comment],
    *,
    open_: bool,
    forms: CommentForms | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    """The definition in a ``<details>``: one row per line with its number, the
    "+" of the line, and the open comments under it. The resolved comments
    fold under the lines.

    ``text`` is the definition of the manager. Without it (an older manager),
    the spec shows as YAML with no line numbers and no form, because a line
    number would not match the lines of the manager."""
    open_comments = [c for c in comments if c.resolved_at is None]
    badge = comments_badge(len(open_comments))
    opened = " open" if open_ or open_comments else ""
    summary = f"<summary>Definition{' ' + badge if badge else ''}</summary>"
    if text is None:
        return (
            f'<details class="a-def"{opened} id="definition">{summary}'
            f"<pre><code>{escape(spec_yaml(spec))}</code></pre></details>"
        )
    lines = text.split("\n") if text else []
    by_line: dict[int, list[Comment]] = {}
    orphans: list[Comment] = []
    for comment in open_comments:
        if 1 <= comment.line_no <= len(lines):
            by_line.setdefault(comment.line_no, []).append(comment)
        else:
            orphans.append(comment)
    rows = []
    for n, line in enumerate(lines, start=1):
        cards = "".join(_comment_card(c, forms=forms, tz=tz, now=now) for c in by_line.get(n, []))
        plus = _add_form(n, line, forms) if forms is not None else ""
        rows.append(
            f'<li class="a-line{" has-comments" if cards else ""}" id="L{n}">'
            f'<a class="a-ln" href="#L{n}">{n}</a>'
            f'<code class="a-code">{escape(line)}</code>{plus}{cards}</li>'
        )
    gone = ""
    if orphans:
        cards = "".join(_comment_card(c, forms=forms, tz=tz, now=now) for c in orphans)
        gone = f'<div class="a-orphans"><p class="j-label">On lines that are gone</p>{cards}</div>'
    resolved = [c for c in comments if c.resolved_at is not None]
    return (
        f'<details class="a-def"{opened} id="definition">{summary}'
        f'<ol class="a-lines">{"".join(rows)}</ol>{gone}'
        f"{_resolved(resolved, tz=tz, now=now)}</details>"
    )


def _definition(
    item: Detail | SavedDetail,
    *,
    open_: bool,
    forms: CommentForms | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    return definition_html(
        item.definition, item.spec, item.comments, open_=open_, forms=forms, tz=tz, now=now
    )


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


# -- the parts of a detail page ---------------------------------------------


# A task box of a rendered plan: ``<li>[x] text`` or, in a loose list,
# ``<li><p>[x] text``. Raw HTML is off, so the box came from the text.
_RENDERED_BOX = re.compile(r"<li>(<p>)?\[([ xX])\] ")


def plan_html(text: str) -> str:
    """The plan as Markdown, with each task box drawn as a box."""

    def box(match: re.Match[str]) -> str:
        done = match.group(2) in "xX"
        css, label = ("a-task done", "done") if done else ("a-task", "to do")
        return (
            f'<li class="{css}">{match.group(1) or ""}'
            f'<span class="a-box" role="img" aria-label="{label}"></span>'
        )

    return _RENDERED_BOX.sub(box, markdown(text))


def _tile(value: str, label: str, *, title: str = "", css: str = "") -> str:
    hint = f' title="{escape(title)}"' if title else ""
    cls = f"j-stat {css}".strip()
    return f'<div class="{cls}"{hint}><b>{value}</b><span>{escape(label)}</span></div>'


def _tiles(tiles: list[str], *, label: str) -> str:
    return f'<div class="j-stats a-stats" role="group" aria-label="{label}">{"".join(tiles)}</div>'


def _breakdown(usage: Usage) -> str:
    return (
        f"input {exact(usage.input)}, output {exact(usage.output)}, "
        f"cache read {exact(usage.cache_read)}, cache write {exact(usage.cache_write)}"
    )


def _hero_tiles(
    item: Detail, *, turns: int | None, outputs: int | None, decisions: int | None
) -> str:
    """The stat row of the hero: short numbers, each with a label."""
    tiles = [
        _tile(
            escape(short(item.usage.total)),
            "tokens",
            title=f"{exact(item.usage.total)} tokens: {_breakdown(item.usage)}",
        ),
        _tile(escape(duration(item.working_s)), "working time"),
    ]
    if turns is not None:
        tiles.append(_tile(str(turns), "turns"))
    if item.model:
        tiles.append(_tile(escape(item.model), "model", css="a-stat-text"))
    if outputs is not None:
        tiles.append(_tile(str(outputs), "outputs" if outputs != 1 else "output"))
    if decisions is not None:
        tiles.append(_tile(str(decisions), "decisions" if decisions != 1 else "decision"))
    return _tiles(tiles, label="Numbers")


def _hero(
    item: Detail, *, lede: str, generated: bool, tiles: str, tz: tzinfo, now: datetime
) -> str:
    """The head of the page: the state, the title, the lede, the stat row."""
    saved = (
        f'<a class="j-chip a-saved" href="/assignments/saved/{item.saved.id}">'
        f"saved: {escape(item.saved.name)}</a>"
        if item.saved
        else ""
    )
    when = _when(item, tz=tz, now=now)
    if item.finished_at:
        when = f"ended {time_html(item.finished_at, tz=tz, now=now)}"
    meta = f'<span class="j-meta">{when}</span>' if when else ""
    css = "a-lede generated" if generated else "a-lede"
    lede_html = f'<p class="{css}">{_inline(lede)}</p>' if lede else ""
    return (
        '<header class="a-hero">'
        f'<div class="a-chips">{status_chip(item.status)}{saved}{meta}</div>'
        f'<h1 class="j-doc-title">{escape(item.title)}</h1>{lede_html}{tiles}</header>'
    )


def _highlights(section: rp.Section | None) -> str:
    """The highlights as cards: the bold lead large, the sentence under it."""
    if section is None or section.empty:
        return ""
    items = section.items
    cards = []
    for entry in items:
        if entry.lead:
            body = f'<p class="a-hl-text">{_inline(entry.body)}</p>' if entry.body else ""
            cards.append(
                f'<li class="a-hl"><strong class="a-hl-lead">{_inline(entry.lead)}</strong>'
                f"{body}</li>"
            )
        else:
            cards.append(f'<li class="a-hl"><p class="a-hl-text">{_inline(entry.text)}</p></li>')
    grid = f'<ul class="a-cards a-hls">{"".join(cards)}</ul>' if cards else ""
    rest = f'<div class="j-prose">{markdown(section.rest)}</div>' if section.rest else ""
    return f'<section class="a-highlights" aria-label="Highlights">{grid}{rest}</section>'


def _decision_items(section: rp.Section | None) -> list[rp.Item]:
    if section is None or section.empty:
        return []
    return [entry for entry in section.items if not rp.is_none(entry.text)]


def _decisions(assignment_id: int, section: rp.Section | None) -> str:
    """The decisions as numbered cards, each with a copy button for Joshua.

    The button is hidden; :data:`COPY_SCRIPT` shows it when the browser can
    copy, so the page works the same with no script."""
    items = _decision_items(section)
    if section is None or (not items and section.empty):
        return ""
    cards = []
    for number, entry in enumerate(items, start=1):
        question, context = rp.decision_question(entry)
        copy = f"Assignment {assignment_id}, decision {number}: {rp.plain(question)}"
        ctx = f'<div class="j-prose a-ctx">{markdown(context)}</div>' if context else ""
        cards.append(
            f'<li class="a-decision"><span class="a-n" aria-hidden="true">{number}</span>'
            f'<div class="a-decision-main"><p class="a-q">{_inline(question)}</p>{ctx}'
            '<div class="a-decision-foot"><span class="j-faint">Tell Joshua your answer.</span>'
            f'<button type="button" class="j-btn sm a-copy" data-copy="{escape(copy)}" hidden>'
            "Copy</button></div></div></li>"
        )
    body = f'<ol class="a-decisions">{"".join(cards)}</ol>' if cards else ""
    if section.rest and not rp.is_none(section.rest):
        body += f'<div class="j-prose">{markdown(section.rest)}</div>'
    count = _count(len(items), "decision") if items else ""
    return _section("Decisions for you", body, count=count, css="a-section a-decide")


# The longest goal, in lines and in characters, that shows open.
_ASKED_LINES = 6
_ASKED_CHARS = 600


def _asked(section: rp.Section | None) -> str:
    """What was asked, as a quote. A long one is folded."""
    if section is None or section.empty:
        return ""
    quote = f'<blockquote class="j-prose a-quote">{markdown(section.body)}</blockquote>'
    if section.body.count("\n") < _ASKED_LINES and len(section.body) <= _ASKED_CHARS:
        return _section("What was asked", quote)
    hint = rp.plain(" ".join(section.body.split()))[:120]
    return (
        '<details class="a-fold"><summary><span class="a-fold-title">What was asked</span>'
        f'<span class="a-fold-hint">{escape(hint)}…</span></summary>'
        f'<div class="a-fold-body">{quote}</div></details>'
    )


def _done(report: rp.Report) -> str:
    """What was done as prose, with the text before the first section after
    the lede and each unknown section under it."""
    parts = []
    section = report.get(rp.DONE)
    if section is not None and not section.empty:
        parts.append(section.body)
    if report.preamble:
        parts.append(report.preamble)
    for extra in report.extra:
        parts.append(f"## {extra.title}\n\n{extra.body}")
    if not parts:
        return ""
    # A stop report holds the plan, so the task boxes are drawn here too.
    prose = plan_html("\n\n".join(parts))
    return _section("What was done", f'<article class="j-prose">{prose}</article>')


def _follow_ups(section: rp.Section | None) -> str:
    """The follow-ups as a static checklist."""
    if section is None or section.empty:
        return ""
    items = [entry for entry in section.items if not rp.is_none(entry.text)]
    body = ""
    if items:
        rows = "".join(
            f'<li><span class="a-box" aria-hidden="true"></span>'
            f"<span>{_inline(entry.text)}</span></li>"
            for entry in items
        )
        body = f'<ul class="a-checklist">{rows}</ul>'
    if section.rest and not rp.is_none(section.rest):
        body += f'<div class="j-prose">{markdown(section.rest)}</div>'
    return _section("Follow-ups", body) if body else ""


def _cost_footer(item: Detail, *, turns: int | None) -> str:
    """The cost again, at the foot, with the full numbers."""
    usage = item.usage
    tiles = [
        _tile(exact(usage.total), "tokens in all"),
        _tile(exact(usage.input), "input"),
        _tile(exact(usage.output), "output"),
        _tile(exact(usage.cache_read), "cache read"),
        _tile(exact(usage.cache_write), "cache write"),
    ]
    if turns is not None:
        tiles.append(_tile(exact(turns), "turns"))
    tiles.append(_tile(escape(duration(item.working_s)), "working time"))
    if item.model:
        tiles.append(_tile(escape(item.model), "model", css="a-stat-text"))
    return (
        '<footer class="a-foot"><h2 class="j-label">Cost</h2>'
        f"{_tiles(tiles, label='Cost')}</footer>"
    )


def _bar(progress: rp.PlanProgress, css: str = "a-meter") -> str:
    """The progress of the plan as a bar, in inline SVG."""
    width = f"{progress.percent:.1f}".rstrip("0").rstrip(".")
    return (
        f'<svg class="{css}" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">'
        '<rect class="track" width="100" height="6" rx="3"/>'
        f'<rect class="fill" width="{width}" height="6" rx="3"/></svg>'
    )


def _steps(progress: rp.PlanProgress) -> str:
    return f"{progress.done} of {progress.total} step" + ("" if progress.total == 1 else "s")


def _now_strip(
    progress: rp.PlanProgress, checkpoints: list[Checkpoint], *, tz: tzinfo, now: datetime
) -> str:
    """While in progress: the bar of the plan, the next step, the last checkpoint."""
    parts = []
    if progress.total:
        parts.append(
            '<div class="a-progress">'
            f'<span class="a-steps"><b>{progress.done}</b> of {progress.total} step'
            f"{'' if progress.total == 1 else 's'}</span>"
            f"{_bar(progress)}</div>"
        )
    if progress.next_step:
        parts.append(
            '<p class="a-now-line"><span class="j-label">Next</span>'
            f"<span>{_inline(progress.next_step)}</span></p>"
        )
    if checkpoints:
        last = checkpoints[0]
        age = time_html(last.at, tz=tz, now=now)
        suffix = f' <span class="j-faint">· {age}</span>' if age else ""
        parts.append(
            '<p class="a-now-line"><span class="j-label">Last checkpoint</span>'
            f"<span>{escape(last.note)}{suffix}</span></p>"
        )
    if not parts:
        return ""
    return f'<section class="a-now" aria-label="Progress now">{"".join(parts)}</section>'


def _fold(title: str, body: str, *, hint: str = "", css: str = "a-fold") -> str:
    """A closed ``<details>`` with ``title`` and a short hint in its summary."""
    return (
        f'<details class="{css}"><summary><span class="a-fold-title">{escape(title)}</span>'
        f"{hint}</summary>"
        f'<div class="a-fold-body">{body}</div></details>'
    )


def _plan_fold(item: Detail, progress: rp.PlanProgress, *, tz: tzinfo, now: datetime) -> str:
    if not (item.plan and item.plan.strip()):
        return ""
    hint = ""
    if progress.total:
        steps = f'<span class="a-fold-hint">{_steps(progress)}</span>'
        hint = _bar(progress, "a-meter mini") + steps
    stamp = time_html(item.plan_at, tz=tz, now=now)
    when = f'<p class="j-meta">updated {stamp}</p>' if stamp else ""
    body = f'{when}<article class="j-prose">{plan_html(item.plan)}</article>'
    return _fold("Plan", body, hint=hint)


def _progress_fold(checkpoints: list[Checkpoint], *, tz: tzinfo, now: datetime) -> str:
    if not checkpoints:
        return ""
    hint = f'<span class="a-fold-hint">{_count(len(checkpoints), "checkpoint")}</span>'
    return _fold("Progress", _progress(checkpoints, tz=tz, now=now), hint=hint)


def _error(item: Detail) -> str:
    if not item.error:
        return ""
    return _section("Error", f'<pre class="a-error">{escape(item.error)}</pre>')


def _crumbs() -> str:
    return '<nav class="j-crumbs"><a href="/assignments">Assignments</a></nav>'


def _turns(item: Detail, report: rp.Report) -> int | None:
    """The turns: ``turns`` of the manager, else the turns that the Cost
    section of the report gives, else None."""
    if item.turns is not None:
        return item.turns
    cost = report.get(rp.COST)
    return rp.turns(cost.body if cost else None)


def _report_page(
    item: Detail,
    report: rp.Report,
    *,
    tz: tzinfo,
    now: datetime,
    forms: CommentForms | None = None,
) -> str:
    """A completed assignment with a report: the report is the page."""
    cards = output_cards(item.outputs, report)
    turns = _turns(item, report)
    decisions = len(_decision_items(report.get(rp.DECISIONS)))
    tiles = _hero_tiles(item, turns=turns, outputs=len(cards), decisions=decisions)
    progress = rp.plan_progress(item.plan)
    parts = [
        _crumbs(),
        _hero(item, lede=report.lede, generated=report.generated, tiles=tiles, tz=tz, now=now),
        _notice(item, tz=tz, now=now),
        _highlights(report.get(rp.HIGHLIGHTS)),
        _decisions(item.id, report.get(rp.DECISIONS)),
        _outputs(cards),
        _asked(report.get(rp.ASKED)),
        _done(report),
        _follow_ups(report.get(rp.FOLLOW_UPS)),
        _cost_footer(item, turns=turns),
        '<div class="a-folds">',
        _plan_fold(item, progress, tz=tz, now=now),
        _progress_fold(item.checkpoints, tz=tz, now=now),
        _definition(item, open_=False, forms=forms, tz=tz, now=now),
        "</div>",
        _error(item),
        COPY_SCRIPT_TAG if decisions else "",
    ]
    return "".join(part for part in parts if part)


def _plain_report_page(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """A completed assignment whose report has none of the known headings:
    the report as Markdown, then the outputs."""
    cards = output_cards(item.outputs, None)
    progress = rp.plan_progress(item.plan)
    stamp = time_html(item.report_at, tz=tz, now=now)
    parts = [
        _crumbs(),
        _hero(
            item,
            lede="",
            generated=False,
            tiles=_hero_tiles(item, turns=item.turns, outputs=len(cards), decisions=None),
            tz=tz,
            now=now,
        ),
        _notice(item, tz=tz, now=now),
        _section("Report", _prose(item.report, ""), stamp=stamp),
        _outputs(cards),
        '<div class="a-folds">',
        _plan_fold(item, progress, tz=tz, now=now),
        _progress_fold(item.checkpoints, tz=tz, now=now),
        _definition(item, open_=False, forms=forms, tz=tz, now=now),
        "</div>",
        _error(item),
    ]
    return "".join(part for part in parts if part)


def _progress_page(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """An assignment with no report yet, or one in progress: the plan is the page."""
    cards = output_cards(item.outputs, None)
    progress = rp.plan_progress(item.plan)
    plan = (
        f'<article class="j-prose">{plan_html(item.plan)}</article>'
        if item.plan and item.plan.strip()
        else '<p class="j-faint">No plan yet.</p>'
    )
    report = ""
    if item.report and item.report.strip():
        report = _fold("Report", f'<article class="j-prose">{markdown(item.report)}</article>')
    parts = [
        _crumbs(),
        _hero(
            item,
            lede="",
            generated=False,
            tiles=_hero_tiles(item, turns=item.turns, outputs=len(cards) or None, decisions=None),
            tz=tz,
            now=now,
        ),
        _notice(item, tz=tz, now=now),
        _now_strip(progress, item.checkpoints, tz=tz, now=now),
        _section("Plan", plan, stamp=time_html(item.plan_at, tz=tz, now=now)),
        _outputs(cards, title="Outputs so far" if item.state != "completed" else "Outputs"),
        _section("Progress", _progress(item.checkpoints, tz=tz, now=now)),
        '<div class="a-folds">',
        report,
        _definition(item, open_=False, forms=forms, tz=tz, now=now),
        "</div>",
        _error(item),
    ]
    return "".join(part for part in parts if part)


def detail_html(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """The body of ``/assignments/{id}``: a document, top to bottom.

    A completed assignment with a report shows the report: a hero, the
    highlights, the decisions, the outputs, what was asked and done, the
    follow-ups, and the cost, with the plan, the progress, and the definition
    folded under it. Any other assignment shows its plan, with the progress
    of the plan and the outputs so far. With ``forms``, each line of the
    definition has a form for a comment."""
    if item.state == "completed" and item.report and item.report.strip():
        report = rp.parse(item.report)
        if report.structured:
            return _report_page(item, report, tz=tz, now=now, forms=forms)
        return _plain_report_page(item, tz=tz, now=now, forms=forms)
    return _progress_page(item, tz=tz, now=now, forms=forms)


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


def refresh_meta(item: Detail) -> str:
    """The ``<meta>`` refresh while the assignment is in progress, else ""."""
    if item.state != "in_progress":
        return ""
    return f'<meta http-equiv="refresh" content="{REFRESH_S}">'


def saved_row(saved: SavedItem, *, tz: tzinfo, now: datetime) -> str:
    """One saved assignment: the name as a link, the title, the runs, the last run."""
    runs = f"{saved.runs} run" + ("" if saved.runs == 1 else "s")
    last = ""
    chip = ""
    if saved.last_run is not None:
        ended = time_html(saved.last_run.finished_at, tz=tz, now=now)
        last = f'<a href="/assignments/{saved.last_run.id}">last run</a>' + (
            f" {ended}" if ended else ""
        )
        chip = status_chip(saved.last_run.status)
    title = f'<span class="j-list-sub">{escape(saved.title)}</span>' if saved.title else ""
    return (
        '<li class="j-list-row a-row"><div class="j-list-main">'
        f'<a class="j-list-title wrap" href="/assignments/saved/{saved.id}">'
        f"{escape(saved.name)}</a>{title}"
        f'<span class="j-list-sub">{_sub([runs, last])}</span></div>'
        f'<div class="a-chips">{comments_badge(saved.open_comments)}{chip}</div></li>'
    )


def _saved_head(extra: str = "") -> str:
    return f'<nav class="j-crumbs"><a href="/assignments">Assignments</a>{extra}</nav>'


def saved_list_html(body: SavedList | None, *, configured: bool, tz: tzinfo, now: datetime) -> str:
    """The body of ``/assignments/saved``."""
    head = _saved_head() + '<h1 class="j-doc-title">Saved assignments</h1>'
    if not configured:
        return head + _not_configured()
    if body is None:
        return head + _no_answer()
    if not body.items:
        return (
            f'{head}<p class="j-empty">No saved assignment. Ask Joshua to save an '
            "assignment by name to run it again.</p>"
        )
    rows = "".join(saved_row(saved, tz=tz, now=now) for saved in body.items)
    return f'{head}<ul class="j-list a-list">{rows}</ul>'


def saved_detail_html(
    saved: SavedDetail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """The body of ``/assignments/saved/{id}``: the definition open, then the runs.
    With ``forms``, each line of the definition has a form for a comment."""
    head = _saved_head(' <span aria-hidden="true">/</span> <a href="/assignments/saved">Saved</a>')
    title = f'<p class="a-lead">{escape(saved.title)}</p>' if saved.title else ""
    updated = time_html(saved.updated_at, tz=tz, now=now)
    meta = _sub(
        [
            f"{saved.runs} run" + ("" if saved.runs == 1 else "s"),
            f"updated {updated}" if updated else "",
        ]
    )
    runs = (
        _rows(saved.runs_list, tz=tz, now=now)
        if saved.runs_list
        else '<p class="j-faint">No run yet.</p>'
    )
    return (
        f'{head}<h1 class="j-doc-title">{escape(saved.name)}</h1>{title}'
        f'<p class="j-meta">{meta}</p>'
        f"{_definition(saved, open_=True, forms=forms, tz=tz, now=now)}"
        f"{_section('Runs', runs)}"
    )


def message_html(title: str, body: str) -> str:
    """A page body with a title and one message, for a manager that is not
    configured or did not answer. ``body`` is HTML."""
    return (
        f'<nav class="j-crumbs"><a href="/assignments">Assignments</a></nav>'
        f'<h1 class="j-doc-title">{escape(title)}</h1>{body}'
    )


def unavailable_html(title: str, *, configured: bool) -> str:
    """The body of a detail page when the manager is not configured or did not
    answer."""
    return message_html(title, _no_answer() if configured else _not_configured())
