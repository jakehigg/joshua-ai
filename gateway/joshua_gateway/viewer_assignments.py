"""The assignments section of the viewer: calls to the manager, and their HTML.

The assignments container keeps each assignment of a person: the
definition, the state, the plan, the progress, and at the end the report. The
viewer asks the read API of the manager (``GET /v1/assignments``,
``GET /v1/assignments/{id}``, ``GET /v1/saved``, ``GET /v1/saved/{id}``) with
its own fleet token, and names the person who signed in as ``person``. The
manager shows a person only their own assignments.

A person reads an assignment here and tells Joshua what to change. The one
write is a comment on a line of a text of the assignment, its ``target``:
the definition, the plan, or the report. The definition shows as a
document: the fields as a list of properties, then the goal as Markdown, cut
into blocks by ``viewer_blocks``. Each field and each block has a "+" that
opens a plain form for its first line, and a ``Source`` fold gives each line
of the text with its number and its own "+". The viewer sends the comment to
the manager as the person who signed in (``POST /v1/assignments/{id}/comments``
or ``POST /v1/saved/{id}/comments``), and a Delete button of an open comment
deletes it. Joshua reads the comments, changes the spec, and resolves them.
The line numbers count the lines of the ``definition`` text of the manager,
so a comment shows under the field or the block that holds its line. The
plan and the report take comments the same way: each block has a "+", and
the form sends ``target`` with it. The ids of their units start with ``P``
and ``R``, so they differ from the ``L`` ids of the definition.

The viewer needs ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either
one missing, :func:`from_env` returns None and the pages say so. A call that
fails returns None, and the page says that the manager did not answer and
shows nothing old. A ``404`` returns :data:`NOT_FOUND`. Each page answers 200
in these cases, except a detail page that the manager does not know, which is
404.

Every string from the manager is untrusted, and the HTML escapes each one. The
plan and the report are Markdown, rendered as a wiki page is, with raw HTML
off, in the same blocks as the goal. A reference of an output
is a link only when it is an ``http`` or ``https`` URL with a host. The
layout has no table: the texts are long, so a
detail page is a document.

A draft has not run. Its page is the definition, open, with the "+" of each
line: the person reads it and leaves comments, and Joshua starts it when the
person says so. A draft has no plan, no report, and no progress. The page and
the row of a draft give the words to say, "Tell Joshua: start assignment N",
so Joshua starts that draft and not a copy of it.

A completed assignment with a report shows the report as the page.
``viewer_report`` cuts the report at its fixed headings, and each section
gets its own element: a hero with the lede and a stat row, a lead band with
the decisions that wait for the person and the outputs as cards (the
documents first, each with an Open link), the highlights as cards, the
decisions as numbered cards, what was asked, what was done, the follow-ups,
and the cost. Each decision card takes an answer: a plain form that stores a
comment on the report, on the first line of the decision. The card then
shows the answer with "Sent to Joshua" and its time, and Joshua's
resolution when it is resolved. The manager sends the answer to Joshua. The plan, the
progress, and the definition fold under it. A report with none of the
headings shows as Markdown. Any other assignment shows its plan, the bar of
its task boxes, the next step, the last checkpoint, and the outputs so far.
A page of an assignment that is in progress refreshes itself every 20
seconds with a ``<meta>`` element.

The blocks and the showcase of a report are in ``viewer_showcase``, and the
"+", the forms, and the comment cards are in ``viewer_comments``. The
documents section uses the same two modules. The pages of this section have
no script.

A re-run is a run of the same assignment. When a run has a lineage (the runs
that share its root), the hero says "run k of assignment N", with N the
root, and a fold lists each run of the lineage. A resolved comment shows
what Joshua changed (``applied``) under the resolution.

The functions here build data and HTML only. The routes live in ``viewer``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from html import escape
from typing import Any, Final
from urllib.parse import urlencode

import httpx
import yaml
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_gateway import viewer_blocks as vb
from joshua_gateway import viewer_report as rp
from joshua_gateway import viewer_showcase as sc
from joshua_gateway.viewer_comments import (
    CommentForms,
    Gutter,
    comments_badge,
    time_html,
)
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
STATES = (("draft", "Drafts"), ("in_progress", "In progress"), ("completed", "Completed"))
# What the list says when a state has no assignment.
_EMPTY = {
    "draft": "No draft.",
    "in_progress": "No assignment is in progress.",
    "completed": "No assignment is completed.",
}
_STATE_KEYS = frozenset(key for key, _ in STATES)

# The badge class of each status of the store. An unknown status is plain.
_STATUS_CLASS = {
    "draft": "plain",
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


class RunRef(_Model):
    id: int
    title: str = ""


class LineageRun(_Model):
    id: int
    title: str = ""
    status: str = ""
    state: str = ""
    parent_id: int | None = None
    created_at: str | None = None
    finished_at: str | None = None


class OpenRequest(_Model):
    # repo, file, or info (a question).
    kind: str = ""
    ref: str = ""
    reason: str = ""


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
    open_request: OpenRequest | None = None
    asked_at: str | None = None
    stopped_at_limits: dict[str, Any] | None = None
    saved: SavedRef | None = None
    parent_id: int | None = None


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
    line_now: str | None = None
    body: str = ""
    created_at: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None
    applied: list[str] = Field(default_factory=list)
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
    rerun_of: RunRef | None = None
    lineage: list[LineageRun] = Field(default_factory=list)


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
        self,
        person: str,
        kind: str,
        parent_id: int,
        *,
        line_no: int,
        line_text: str,
        body: str,
        target: str = "definition",
    ) -> Written | None:
        """Write a comment of ``person`` on line ``line_no`` of a text of the
        assignment: its definition, its plan, or its report (``target``)."""
        path = f"{KINDS[kind]}/{parent_id}/comments"
        fields: dict[str, Any] = {"line_no": line_no, "line_text": line_text, "body": body}
        if target != "definition":
            fields["target"] = target
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
    tokens = (
        f'<span title="{exact(item.usage.total)} tokens">{short(item.usage.total)} tokens</span>'
        if item.state != "draft"
        else ""
    )
    sub = _sub(
        [
            _when(item, tz=tz, now=now),
            escape(item.model),
            tokens,
            sc.count(outputs, "output") if outputs else "",
        ]
    )
    lede = f'<span class="a-row-line">{sc.inline(line)}</span>' if line else ""
    if item.state == "draft":
        lede += f'<span class="a-row-line j-muted">{escape(start_phrase(item.id))}</span>'
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
    head = f'<h1 class="j-doc-title">assignments</h1>{_pills(state)}'
    if not configured:
        return head + _not_configured()
    if body is None:
        return head + _no_answer()
    if not body.items:
        text = _EMPTY.get(state or "", "No assignment here.")
        return f'{head}<p class="j-empty">{escape(text)}</p>'
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


# The heading of the notice of a request, for each kind but a question.
REQUEST_HEADINGS = {"repo": "requests a repository", "file": "requests a file"}


def _request_notice(request: OpenRequest, when: str) -> str:
    reason = (
        f'<p class="a-text">Reason: {escape(request.reason)}</p>' if request.reason.strip() else ""
    )
    return (
        '<aside class="a-notice warn" role="note">'
        f"<strong>The assignment {REQUEST_HEADINGS[request.kind]}{when}</strong>"
        f'<p class="a-text"><code>{escape(request.ref)}</code></p>{reason}'
        '<p class="j-muted">Joshua grants it, provides it, or refuses it.</p></aside>'
    )


def _notice(item: Detail, *, tz: tzinfo, now: datetime) -> str:
    request = item.open_request
    if request is not None and request.kind in REQUEST_HEADINGS:
        asked = time_html(item.asked_at, tz=tz, now=now)
        return _request_notice(request, f" {asked}" if asked else "")
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


def _prose(text: str | None, empty: str) -> str:
    if text and text.strip():
        return sc.article(sc.doc_html(text))
    return f'<p class="j-faint">{escape(empty)}</p>'


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


@dataclass(frozen=True)
class Unit:
    """One commentable part of a definition: lines ``first`` to ``last`` (from
    1) of the text, its HTML, its CSS class, and what its "+" names."""

    first: int
    last: int
    body: str
    css: str
    what: str


def _label(key: str) -> str:
    return key.replace("_", " ")


def _tag_text(value: Any) -> str:
    """One value in a list or a mapping, as plain text. Not escaped."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "none"
    if isinstance(value, str | int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _tags(values: list[str]) -> str:
    tags = "".join(f'<span class="a-tag">{escape(v)}</span>' for v in values)
    return f'<span class="a-tags">{tags}</span>'


_NONE_HTML = '<span class="j-faint">none</span>'


def prop_value(item: vb.Field) -> str:
    """The value of a field of a definition, as HTML: text, yes or no, tags
    for a list or a mapping, and the source lines when YAML cannot read them."""
    if not item.parsed:
        rest = "\n".join(item.lines)
        return f'<code class="a-code">{escape(rest)}</code>'
    value = item.value
    if isinstance(value, list | dict) and not value:
        return _NONE_HTML
    if isinstance(value, list):
        return _tags([_tag_text(v) for v in value])
    if isinstance(value, dict):
        return _tags([f"{_label(str(k))}: {_tag_text(v)}" for k, v in value.items()])
    if value is None or value == "":
        return _NONE_HTML
    return f'<span class="a-prop-text">{escape(_tag_text(value))}</span>'


def _prop(item: vb.Field, *, label: str, value: str) -> Unit:
    body = (
        f'<span class="a-prop-key">{escape(label)}</span><span class="a-prop-value">{value}</span>'
    )
    return Unit(item.first, item.last, body, "a-prop", f"the field {label or 'here'}")


def definition_units(text: str) -> tuple[list[Unit], list[Unit]]:
    """The fields of a definition (all but the goal), and the goal: its head,
    then one unit a block of its Markdown. A goal that is not a block scalar
    is a field."""
    props: list[Unit] = []
    goal: list[Unit] = []
    for item in vb.fields(text):
        found = vb.goal_offset(item) if item.key == "goal" else None
        if found is None:
            props.append(_prop(item, label=_label(item.key), value=prop_value(item)))
            continue
        parts = vb.blocks(found.markdown)
        head_last = found.header + parts[0].first - 1 if parts else found.last
        goal.append(
            Unit(
                found.header,
                head_last,
                '<span class="a-prop-key">goal</span>',
                "a-goal-head",
                "the goal",
            )
        )
        for part in parts:
            first = found.header + part.first
            goal.append(
                Unit(
                    first,
                    found.header + part.last,
                    part.html,
                    f"a-block {part.kind} d{min(part.depth, vb.MAX_DEPTH)}",
                    f"the {sc.BLOCK_NAMES.get(part.kind, 'block')} at line {first}",
                )
            )
    return props, goal


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
    """The definition in a ``<details>``, as a document: the fields as a list
    of properties, then the goal as Markdown, one block a unit. Each unit has
    the "+" of its first line and shows the open comments on any of its
    lines. A ``Source`` fold under it gives each line with its number and its
    "+". The resolved comments fold at the end.

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
    gutter = Gutter(text.split("\n") if text else [], comments, forms, tz, now)
    props, goal = definition_units(text)

    def units(part: list[Unit]) -> str:
        return "".join(gutter.unit(u.first, u.last, u.body, u.css, u.what) for u in part)

    doc = ""
    if props:
        doc += f'<div class="a-props">{units(props)}</div>'
    if goal:
        doc += f'<div class="a-goal j-prose">{units(goal)}</div>'
    if not doc:
        doc = '<p class="j-faint a-def-empty">The definition is empty.</p>'
    return (
        f'<details class="a-def"{opened} id="definition">{summary}'
        f'<div class="a-doc">{doc}</div>{gutter.orphans()}{gutter.source()}'
        f"{gutter.resolved()}</details>"
    )


def _definition(
    item: Detail | SavedDetail,
    *,
    open_: bool,
    forms: CommentForms | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    comments = [c for c in item.comments if c.target == "definition"]
    return definition_html(
        item.definition, item.spec, comments, open_=open_, forms=forms, tz=tz, now=now
    )


# The texts of a run that take comments beside its definition, and the start
# of the ids of their units.
TEXT_PREFIX = {"plan": "P", "report": "R"}


def text_lines(text: str | None) -> list[str]:
    """The lines of a plan or a report, as the manager counts them."""
    return text.replace("\r\n", "\n").split("\n") if text else []


def text_gutter(
    item: Detail, target: str, forms: CommentForms | None, *, tz: tzinfo, now: datetime
) -> Gutter:
    """The gutter of the plan or the report of ``item``: its lines, its
    comments, and forms that send ``target``."""
    text = item.plan if target == "plan" else item.report
    # A manager that gives no definition takes no comment on a line.
    can_write = forms is not None and item.definition is not None
    return Gutter(
        text_lines(text),
        [c for c in item.comments if c.target == target],
        replace(forms, target=target) if can_write and forms is not None else None,
        tz,
        now,
        prefix=TEXT_PREFIX[target],
    )


# -- the parts of a detail page ---------------------------------------------


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
        sc.tile(
            escape(short(item.usage.total)),
            "tokens",
            title=f"{exact(item.usage.total)} tokens: {_breakdown(item.usage)}",
        ),
        sc.tile(escape(duration(item.working_s)), "working time"),
    ]
    if turns is not None:
        tiles.append(sc.tile(str(turns), "turns"))
    if item.model:
        tiles.append(sc.tile(escape(item.model), "model", css="a-stat-text"))
    if outputs is not None:
        tiles.append(sc.tile(str(outputs), "outputs" if outputs != 1 else "output"))
    if decisions is not None:
        tiles.append(sc.tile(str(decisions), "decisions" if decisions != 1 else "decision"))
    return sc.tiles(tiles, label="Numbers")


def run_number(item: Detail) -> tuple[int, int] | None:
    """``(k, root)``: this run is run k of the lineage of the assignment
    ``root``. None when the run has no other run in its lineage."""
    ids = [run.id for run in item.lineage]
    if len(ids) < 2 or item.id not in ids:
        return None
    return ids.index(item.id) + 1, ids[0]


def run_chip(item: Detail) -> str:
    """The chip "run k of assignment N" of a run in a lineage, else ""."""
    found = run_number(item)
    if found is None:
        return ""
    k, root = found
    return f'<a class="j-chip a-run" href="#lineage">run {k} of assignment {root}</a>'


def _lineage_fold(item: Detail, *, tz: tzinfo, now: datetime) -> str:
    """Each run of the lineage, the oldest first, in a small fold. "" for a
    run with no other run."""
    if run_number(item) is None:
        return ""
    rows = []
    for k, run in enumerate(item.lineage, start=1):
        when = time_html(run.finished_at or run.created_at, tz=tz, now=now)
        stamp = f'<span class="j-faint">{when}</span>' if when else ""
        name = f"Run {k}: assignment {run.id}"
        link = (
            f'<b aria-current="page">{escape(name)} (this run)</b>'
            if run.id == item.id
            else f'<a href="/assignments/{run.id}">{escape(name)}</a>'
        )
        title = f'<span class="j-list-sub">{escape(run.title)}</span>' if run.title else ""
        rows.append(
            f'<li class="a-lineage-run">{link}{title}'
            f'<span class="a-chips">{status_chip(run.status)}{stamp}</span></li>'
        )
    hint = f'<span class="a-fold-hint">{sc.count(len(item.lineage), "run")}</span>'
    return sc.fold(
        "Runs of this assignment",
        f'<ol class="a-lineage">{"".join(rows)}</ol>',
        hint=hint,
        fid="lineage",
    )


def _hero(
    item: Detail, *, lede: str, generated: bool, tiles: str, tz: tzinfo, now: datetime
) -> str:
    """The head of the page: the state, the title, the lede, the stat row."""
    saved = (
        f'<a class="j-chip a-saved" href="/assignments/saved/{item.saved.id}">'
        f"saved: {escape(item.saved.name)}</a>"
        if item.saved
        else ""
    ) + run_chip(item)
    when = _when(item, tz=tz, now=now)
    if item.finished_at:
        when = f"ended {time_html(item.finished_at, tz=tz, now=now)}"
    meta = f'<span class="j-meta">{when}</span>' if when else ""
    css = "a-lede generated" if generated else "a-lede"
    lede_html = f'<p class="{css}">{sc.inline(lede)}</p>' if lede else ""
    return (
        '<header class="a-hero">'
        f'<div class="a-chips">{status_chip(item.status)}{saved}{meta}</div>'
        f'<h1 class="j-doc-title">{escape(item.title)}</h1>{lede_html}{tiles}</header>'
    )


def _cost_footer(item: Detail, *, turns: int | None) -> str:
    """The cost again, at the foot, with the full numbers."""
    usage = item.usage
    tiles = [
        sc.tile(exact(usage.total), "tokens in all"),
        sc.tile(exact(usage.input), "input"),
        sc.tile(exact(usage.output), "output"),
        sc.tile(exact(usage.cache_read), "cache read"),
        sc.tile(exact(usage.cache_write), "cache write"),
    ]
    if turns is not None:
        tiles.append(sc.tile(exact(turns), "turns"))
    tiles.append(sc.tile(escape(duration(item.working_s)), "working time"))
    if item.model:
        tiles.append(sc.tile(escape(item.model), "model", css="a-stat-text"))
    return (
        '<footer class="a-foot"><h2 class="j-label">Cost</h2>'
        f"{sc.tiles(tiles, label='Cost')}</footer>"
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
            f"<span>{sc.inline(progress.next_step)}</span></p>"
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


def _plan_fold(
    item: Detail,
    progress: rp.PlanProgress,
    *,
    tz: tzinfo,
    now: datetime,
    forms: CommentForms | None = None,
) -> str:
    """The plan, folded, with the "+" of each block. It is open while it has
    an open comment."""
    if not (item.plan and item.plan.strip()):
        return ""
    hint = ""
    if progress.total:
        steps = f'<span class="a-fold-hint">{_steps(progress)}</span>'
        hint = _bar(progress, "a-meter mini") + steps
    stamp = time_html(item.plan_at, tz=tz, now=now)
    when = f'<p class="j-meta">updated {stamp}</p>' if stamp else ""
    gutter = text_gutter(item, "plan", forms, tz=tz, now=now)
    badge = comments_badge(len(gutter.open))
    plan = sc.article(sc.plan_html(item.plan, live(gutter)))
    body = f"{when}{plan}{gutter.orphans()}{gutter.resolved()}"
    return sc.fold(
        "Plan",
        body,
        hint=hint + (f" {badge}" if badge else ""),
        open_=bool(gutter.open),
        fid="plan",
    )


def _progress_fold(checkpoints: list[Checkpoint], *, tz: tzinfo, now: datetime) -> str:
    if not checkpoints:
        return ""
    hint = f'<span class="a-fold-hint">{sc.count(len(checkpoints), "checkpoint")}</span>'
    return sc.fold("Progress", _progress(checkpoints, tz=tz, now=now), hint=hint)


def _error(item: Detail) -> str:
    if not item.error:
        return ""
    return sc.section("Error", f'<pre class="a-error">{escape(item.error)}</pre>')


def _crumbs() -> str:
    return '<nav class="j-crumbs"><a href="/assignments">assignments</a></nav>'


def _turns(item: Detail, report: rp.Report) -> int | None:
    """The turns: ``turns`` of the manager, else the turns that the Cost
    section of the report gives, else None."""
    if item.turns is not None:
        return item.turns
    cost = report.get(rp.COST)
    return rp.turns(cost.body if cost else None)


def live(gutter: Gutter) -> Gutter | None:
    """``gutter``, or None when it has no form and no comment: then the text
    renders as plain blocks."""
    return gutter if gutter.forms is not None or gutter.comments else None


def _report_gutter(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None
) -> Gutter:
    return text_gutter(item, "report", forms, tz=tz, now=now)


def _outputs_comments(gutter: Gutter, report: rp.Report | None) -> str:
    """The comments on the lines of the Outputs section of the report, which
    the lead band shows in place of the section."""
    part = report.get(rp.OUTPUTS) if report is not None else None
    if part is None or not part.line:
        return ""
    return gutter.take(part.line, max(part.line, part.end))


def _report_rest(gutter: Gutter) -> str:
    """The comments on the report that no part of the page took, and the
    resolved ones, folded."""
    rest = gutter.orphans() + gutter.resolved()
    return f'<div class="a-folds a-report-rest">{rest}</div>' if rest else ""


def _report_page(
    item: Detail,
    report: rp.Report,
    *,
    tz: tzinfo,
    now: datetime,
    forms: CommentForms | None = None,
) -> str:
    """A completed assignment with a report: the report is the page, under a
    lead band with the decisions that wait and the outputs."""
    cards = sc.output_cards(item.outputs, report)
    turns = _turns(item, report)
    decisions_part = report.get(rp.DECISIONS)
    decisions = len(sc.decision_items(decisions_part))
    tiles = _hero_tiles(item, turns=turns, outputs=len(cards), decisions=decisions)
    progress = rp.plan_progress(item.plan)
    full = _report_gutter(item, tz=tz, now=now, forms=forms)
    gutter = live(full)
    lead = sc.lead_html(
        cards,
        waiting=sc.decisions_waiting(decisions_part, full),
        decisions=decisions,
        extra=_outputs_comments(full, report),
    )
    parts = [
        _crumbs(),
        _hero(item, lede=report.lede, generated=report.generated, tiles=tiles, tz=tz, now=now),
        _notice(item, tz=tz, now=now),
        lead,
        sc.highlights_html(report.get(rp.HIGHLIGHTS), gutter),
        sc.decisions_html(f"Assignment {item.id}", decisions_part, gutter, answers=True),
        sc.asked_html(report.get(rp.ASKED), gutter),
        sc.done_html(report, gutter),
        sc.follow_ups_html(report.get(rp.FOLLOW_UPS), gutter),
        _report_rest(full),
        _cost_footer(item, turns=turns),
        '<div class="a-folds">',
        _lineage_fold(item, tz=tz, now=now),
        _plan_fold(item, progress, tz=tz, now=now, forms=forms),
        _progress_fold(item.checkpoints, tz=tz, now=now),
        _definition(item, open_=False, forms=forms, tz=tz, now=now),
        "</div>",
        _error(item),
    ]
    return "".join(part for part in parts if part)


def _plain_report_page(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """A completed assignment whose report has none of the known headings:
    the lead band with the outputs, then the report as blocks."""
    cards = sc.output_cards(item.outputs, None)
    progress = rp.plan_progress(item.plan)
    stamp = time_html(item.report_at, tz=tz, now=now)
    gutter = _report_gutter(item, tz=tz, now=now, forms=forms)
    report = sc.article(sc.blocks_html(item.report or "", gutter=live(gutter)))
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
        sc.lead_html(cards, waiting=0, decisions=0),
        sc.section("Report", report, stamp=stamp),
        _report_rest(gutter),
        '<div class="a-folds">',
        _lineage_fold(item, tz=tz, now=now),
        _plan_fold(item, progress, tz=tz, now=now, forms=forms),
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
    cards = sc.output_cards(item.outputs, None)
    progress = rp.plan_progress(item.plan)
    plan = '<p class="j-faint">No plan yet.</p>'
    if item.plan and item.plan.strip():
        gutter = text_gutter(item, "plan", forms, tz=tz, now=now)
        plan = sc.article(sc.plan_html(item.plan, live(gutter)))
        plan += gutter.orphans() + gutter.resolved()
    report = ""
    if item.report and item.report.strip():
        report = sc.fold("Report", sc.article(sc.doc_html(item.report)))
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
        sc.section("Plan", plan, stamp=time_html(item.plan_at, tz=tz, now=now), sid="plan"),
        sc.outputs_html(cards, title="Outputs so far" if item.state != "completed" else "Outputs"),
        sc.section("Progress", _progress(item.checkpoints, tz=tz, now=now)),
        '<div class="a-folds">',
        _lineage_fold(item, tz=tz, now=now),
        report,
        _definition(item, open_=False, forms=forms, tz=tz, now=now),
        "</div>",
        _error(item),
    ]
    return "".join(part for part in parts if part)


# The line on the page of a draft.
DRAFT_LINE = "Tell Joshua to start it, or leave comments first."


def start_phrase(assignment_id: int) -> str:
    """The words that make Joshua start exactly this draft."""
    return f"Tell Joshua: start assignment {int(assignment_id)}"


def _draft_page(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """A draft: the head, then the definition open with the "+" of each line.

    A draft never ran, so there is no plan, no report, and no progress."""
    saved = (
        f'<a class="j-chip a-saved" href="/assignments/saved/{item.saved.id}">'
        f"saved: {escape(item.saved.name)}</a>"
        if item.saved
        else ""
    )
    when = _when(item, tz=tz, now=now)
    meta = f'<span class="j-meta">{when}</span>' if when else ""
    badge = comments_badge(item.open_comments)
    parts = [
        _crumbs(),
        '<header class="a-hero">'
        f'<div class="a-chips">{status_chip(item.status)}{saved}{badge}{meta}</div>'
        f'<h1 class="j-doc-title">{escape(item.title)}</h1>'
        f'<p class="a-lede a-start-line">{escape(start_phrase(item.id))}</p>'
        f'<p class="a-lede a-draft-line">{escape(DRAFT_LINE)}</p></header>',
        _definition(item, open_=True, forms=forms, tz=tz, now=now),
        _error(item),
    ]
    return "".join(part for part in parts if part)


def detail_html(
    item: Detail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """The body of ``/assignments/{id}``: a document, top to bottom.

    A draft shows its definition open, for comments, and nothing else of a
    run.

    A completed assignment with a report shows the report: a hero, the
    highlights, the decisions, the outputs, what was asked and done, the
    follow-ups, and the cost, with the plan, the progress, and the definition
    folded under it. Any other assignment shows its plan, with the progress
    of the plan and the outputs so far. With ``forms``, each line of the
    definition has a form for a comment."""
    if item.state == "draft":
        return _draft_page(item, tz=tz, now=now, forms=forms)
    if item.state == "completed" and item.report and item.report.strip():
        report = rp.parse(item.report)
        if report.structured:
            return _report_page(item, report, tz=tz, now=now, forms=forms)
        return _plain_report_page(item, tz=tz, now=now, forms=forms)
    return _progress_page(item, tz=tz, now=now, forms=forms)


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


def saved_detail_html(
    saved: SavedDetail, *, tz: tzinfo, now: datetime, forms: CommentForms | None = None
) -> str:
    """The body of ``/assignments/saved/{id}``: the definition open, then the runs.
    With ``forms``, each line of the definition has a form for a comment."""
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
        f"{_definition(saved, open_=True, forms=forms, tz=tz, now=now)}"
        f"{sc.section('Runs', runs)}"
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
