"""The assignments section of the viewer: calls to the manager, and their HTML.

The assignments container keeps each assignment of a person: the
definition, the state, the plan, the progress, and at the end the report. The
viewer asks the read API of the manager (``GET /v1/assignments``,
``GET /v1/assignments/{id}``, ``GET /v1/saved``, ``GET /v1/saved/{id}``) with
its own fleet token, and names the person who signed in as ``person``. The
manager shows a person only their own assignments.

The section is read-only. A person reads an assignment here and tells Joshua
what to change.

The viewer needs ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either
one missing, :func:`from_env` returns None and the pages say so. A call that
fails returns None, and the page says that the manager did not answer and
shows nothing old. A ``404`` returns :data:`NOT_FOUND`. Each page answers 200
in these cases, except a detail page that the manager does not know, which is
404.

Every string from the manager is untrusted, and the HTML escapes each one. The
plan and the report are Markdown, rendered as a wiki page is, with raw HTML
off. A reference of an output is a link only when it is an ``http`` or
``https`` URL. The layout has no table: the texts are long, so a detail page is
a document. A page of an assignment that is in progress refreshes itself
every 20 seconds with a ``<meta>`` element, so the page needs no script.

The functions here build data and HTML only. The routes live in ``viewer``.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    usage: Usage = Field(default_factory=Usage)
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


class Detail(Item):
    spec: dict[str, Any] = Field(default_factory=dict)
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


class SavedList(_Model):
    items: list[SavedItem]


class SavedDetail(SavedItem):
    runs_list: list[Item] = Field(default_factory=list)


# -- the client -------------------------------------------------------------


class NotFound:
    """The manager answered 404: no such row for this person."""

    def __repr__(self) -> str:
        return "NOT_FOUND"


NOT_FOUND: Final = NotFound()


class AssignmentsClient:
    """Read the assignments of one person from the manager."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """The JSON body, :data:`NOT_FOUND` on a 404, or None on a failure."""
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.get_json(path, params=params)
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

    async def _read[M: BaseModel](
        self, model: type[M], path: str, params: dict[str, Any]
    ) -> M | NotFound | None:
        body = await self._get(path, params)
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


def item_row(item: Item, *, tz: tzinfo, now: datetime) -> str:
    """One row: the title as a link, the status, when, the model, the tokens."""
    question = (
        '<span class="j-badge warn plain" title="The assignment asks a question">question</span>'
        if item.open_question
        else ""
    )
    sub = _sub(
        [
            _when(item, tz=tz, now=now),
            escape(item.model),
            f'<span title="{exact(item.usage.total)} tokens">'
            f"{short(item.usage.total)} tokens</span>",
        ]
    )
    return (
        '<li class="j-list-row a-row"><div class="j-list-main">'
        f'<a class="j-list-title wrap" href="/assignments/{item.id}">{escape(item.title)}</a>'
        f'<span class="j-list-sub">{sub}</span></div>'
        f'<div class="a-chips">{question}{status_chip(item.status)}</div></li>'
    )


def _rows(items: list[Item], *, tz: tzinfo, now: datetime) -> str:
    return f'<ul class="j-list a-list">{"".join(item_row(i, tz=tz, now=now) for i in items)}</ul>'


def _pills(current: str) -> str:
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
    body: ItemList | None, *, state: str, configured: bool, tz: tzinfo, now: datetime
) -> str:
    """The body of ``/assignments``. ``body`` None means the manager did not
    answer."""
    head = f'<h1 class="j-doc-title">assignments</h1>{_pills(state)}'
    if not configured:
        return head + _not_configured()
    if body is None:
        return head + _no_answer()
    if not body.items:
        label = dict(STATES)[state].lower()
        return f'{head}<p class="j-empty">No assignment is {escape(label)}.</p>'
    return head + _rows(body.items, tz=tz, now=now)


def _cost_line(item: Item, *, tz: tzinfo, now: datetime) -> str:
    usage = item.usage
    breakdown = (
        f"input {exact(usage.input)}, output {exact(usage.output)}, "
        f"cache read {exact(usage.cache_read)}, cache write {exact(usage.cache_write)}"
    )
    parts = [
        f'<span title="{breakdown}">{exact(usage.total)} tokens</span>',
        f"{duration(item.working_s)} working",
        escape(item.model),
        _when(item, tz=tz, now=now),
    ]
    if item.finished_at:
        parts.append(f"ended {time_html(item.finished_at, tz=tz, now=now)}")
    return f'<p class="j-meta a-cost">{_sub(parts)}</p>'


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


def _section(title: str, body: str, *, stamp: str = "") -> str:
    when = f'<span class="j-meta">updated {stamp}</span>' if stamp else ""
    return (
        f'<section class="a-section"><div class="a-head"><h2>{escape(title)}</h2>{when}</div>'
        f"{body}</section>"
    )


def _prose(text: str | None, empty: str) -> str:
    if text and text.strip():
        return f'<article class="j-prose">{markdown(text)}</article>'
    return f'<p class="j-faint">{escape(empty)}</p>'


def output_item(output: Output) -> str:
    """One output: ``kind: title``, a link when ``ref`` is a URL, else the
    ``ref`` as code after the title."""
    kind = f'<span class="a-kind">{escape(output.kind)}</span>: ' if output.kind else ""
    title = output.title or output.ref
    if is_url(output.ref):
        body = f'<a href="{escape(output.ref.strip())}" rel="noreferrer">{escape(title)}</a>'
    else:
        ref = f" <code>{escape(output.ref)}</code>" if output.ref and output.title else ""
        body = f"{escape(title)}{ref}" if output.title else f"<code>{escape(output.ref)}</code>"
    return f"<li>{kind}{body}</li>"


def _outputs(outputs: list[Output]) -> str:
    if not outputs:
        return ""
    items = "".join(output_item(output) for output in outputs)
    return f'<h3 class="a-sub">Recorded outputs</h3><ul class="a-outputs">{items}</ul>'


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


def _definition(spec: Mapping[str, Any], *, open_: bool) -> str:
    opened = " open" if open_ else ""
    return (
        f'<details class="a-def"{opened}><summary>Definition</summary>'
        f"<pre><code>{escape(spec_yaml(spec))}</code></pre></details>"
    )


def detail_html(item: Detail, *, tz: tzinfo, now: datetime) -> str:
    """The body of ``/assignments/{id}``: a document, top to bottom."""
    saved = (
        f'<a class="j-chip a-saved" href="/assignments/saved/{item.saved.id}">'
        f"saved: {escape(item.saved.name)}</a>"
        if item.saved
        else ""
    )
    parts = [
        '<nav class="j-crumbs"><a href="/assignments">assignments</a></nav>',
        f'<h1 class="j-doc-title">{escape(item.title)}</h1>',
        f'<div class="a-chips">{status_chip(item.status)}{saved}</div>',
        _cost_line(item, tz=tz, now=now),
        _notice(item, tz=tz, now=now),
        _section(
            "Plan",
            _prose(item.plan, "No plan yet."),
            stamp=time_html(item.plan_at, tz=tz, now=now),
        ),
    ]
    if item.report or item.outputs:
        parts.append(
            _section(
                "Report",
                _prose(item.report, "No report yet.") + _outputs(item.outputs),
                stamp=time_html(item.report_at, tz=tz, now=now),
            )
        )
    parts.append(_section("Progress", _progress(item.checkpoints, tz=tz, now=now)))
    parts.append(_definition(item.spec, open_=False))
    if item.error:
        parts.append(_section("Error", f'<pre class="a-error">{escape(item.error)}</pre>'))
    return "".join(part for part in parts if part)


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
        f'<div class="a-chips">{chip}</div></li>'
    )


def _saved_head(extra: str = "") -> str:
    return f'<nav class="j-crumbs"><a href="/assignments">assignments</a>{extra}</nav>'


def saved_list_html(body: SavedList | None, *, configured: bool, tz: tzinfo, now: datetime) -> str:
    """The body of ``/assignments/saved``."""
    head = _saved_head() + '<h1 class="j-doc-title">saved assignments</h1>'
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


def saved_detail_html(saved: SavedDetail, *, tz: tzinfo, now: datetime) -> str:
    """The body of ``/assignments/saved/{id}``: the definition open, then the runs."""
    head = _saved_head(' <span aria-hidden="true">/</span> <a href="/assignments/saved">saved</a>')
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
        f"{_definition(saved.spec, open_=True)}"
        f"{_section('Runs', runs)}"
    )


def message_html(title: str, body: str) -> str:
    """A page body with a title and one message, for a manager that is not
    configured or did not answer. ``body`` is HTML."""
    return (
        f'<nav class="j-crumbs"><a href="/assignments">assignments</a></nav>'
        f'<h1 class="j-doc-title">{escape(title)}</h1>{body}'
    )


def unavailable_html(title: str, *, configured: bool) -> str:
    """The body of a detail page when the manager is not configured or did not
    answer."""
    return message_html(title, _no_answer() if configured else _not_configured())
