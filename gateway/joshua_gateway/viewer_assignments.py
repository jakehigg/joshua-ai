"""The assignments section of the viewer: calls to the manager, and its data.

The assignments container keeps each assignment of a person: the definition,
the state, the plan, the progress, and at the end the report. The viewer asks
the read API of the manager (``GET /v1/assignments``,
``GET /v1/assignments/{id}``, ``GET /v1/saved``, ``GET /v1/saved/{id}``) with
its own fleet token, and names the person who signed in as ``person``. The
manager shows a person only their own assignments.

A person reads an assignment here and tells Joshua what to change. The one
write is a comment on a line of a text of the assignment, its ``target``: the
definition, the plan, or the report. The viewer sends the comment to the
manager as the person who signed in (``POST /v1/assignments/{id}/comments``
or ``POST /v1/saved/{id}/comments``), and the author of an open comment
deletes it. Joshua reads the comments, changes the spec, and resolves them.
The line numbers count the lines of the ``definition``, ``plan`` or
``report`` text of the manager, so a comment shows under the field or the
block that holds its line.

The definition reads as a document: :func:`definition_units` gives the fields
as properties, then the goal cut into blocks by ``viewer_blocks``, each unit
with its first and its last line of the text. The ids of the units of the
plan and the report start with ``P`` and ``R`` (:data:`TEXT_PREFIX`), so they
differ from the ``L`` ids of the definition.

The viewer needs ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either
one missing, :func:`from_env` returns None and the pages say so. A call that
fails returns None, and the page says that the manager did not answer and
shows nothing old. A ``404`` returns :data:`NOT_FOUND`. Each page answers 200
in these cases, except a detail page that the manager does not know, which is
404.

Every string from the manager is untrusted. The assignments, the saved ones,
and each detail are JSON routes (``viewer_api.assignments``) that a React
page reads, and every string a person sees there — the duration, the status
label, the phrase of a start — is derived here.

A draft has not run. It has no plan, no report, and no progress, and it shows
the words to say, "Tell Joshua: start assignment N" (:func:`start_phrase`),
so Joshua starts that draft and not a copy of it.

A re-run is a run of the same assignment. When a run has a lineage (the runs
that share its root), :func:`run_number` gives "run k of assignment N", with
N the root.

The functions here build data only. The routes live in ``viewer_api``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Final

import httpx
import yaml
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_gateway import viewer_blocks as vb
from joshua_gateway.viewer_limits import when_text
from joshua_gateway.viewer_usage import exact

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

# The status of the store as a person reads it. Any other status is its own
# words, with no underscore.
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


def empty_text(state: str | None) -> str:
    """What the list says when a state holds no assignment."""
    return _EMPTY.get(state or "", "No assignment here.")


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


def status_label(status: str) -> str:
    """A status of the store as a person reads it: ``paused at limits``."""
    return _STATUS_LABEL.get(status, status.replace("_", " "))


def summary_line(item: Item) -> str:
    """The line under the title of a row, as Markdown: the lede of the report,
    else the next step of the plan, else "". The manager gives both."""
    if item.lede and item.lede.strip():
        return item.lede.strip()
    step = (item.next_step or "").strip()
    return f"Next: {step}" if step else ""


def limits_text(stop: Mapping[str, Any], *, tz: tzinfo, now: datetime) -> str:
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


def spec_yaml(spec: Mapping[str, Any]) -> str:
    """The definition as YAML, in the order of the manager. Not escaped."""
    text = yaml.safe_dump(dict(spec), sort_keys=False, allow_unicode=True, width=100)
    return text.strip()


@dataclass(frozen=True)
class Unit:
    """One commentable part of a definition: lines ``first`` to ``last`` (from
    1) of the text, its value as data, and what its "+" names.

    ``kind`` is ``prop`` for a field, ``goal`` for the head of the goal, and
    the kind of the block (``p``, ``li``, ``code``, …) for a block of it.
    """

    first: int
    last: int
    what: str
    kind: str = "prop"
    label: str = ""
    value: dict[str, Any] | None = None
    markdown: str = ""
    depth: int = 0


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


def _prop_value(item: vb.Field) -> dict[str, Any]:
    """The value of a field of a definition, as data: ``text``, ``tags`` for a
    list or a mapping, ``none`` for an empty one, and ``code`` with the source
    lines when YAML cannot read them."""
    if not item.parsed:
        return {"kind": "code", "text": "\n".join(item.lines)}
    value = item.value
    if isinstance(value, list | dict) and not value:
        return {"kind": "none"}
    if isinstance(value, list):
        return {"kind": "tags", "tags": [_tag_text(v) for v in value]}
    if isinstance(value, dict):
        return {
            "kind": "tags",
            "tags": [f"{_label(str(k))}: {_tag_text(v)}" for k, v in value.items()],
        }
    if value is None or value == "":
        return {"kind": "none"}
    return {"kind": "text", "text": _tag_text(value)}


def _prop(item: vb.Field) -> Unit:
    label = _label(item.key)
    return Unit(
        item.first,
        item.last,
        f"the field {label or 'here'}",
        kind="prop",
        label=label,
        value=_prop_value(item),
    )


def definition_units(text: str) -> tuple[list[Unit], list[Unit]]:
    """The fields of a definition (all but the goal), and the goal: its head,
    then one unit a block of its Markdown. A goal that is not a block scalar
    is a field."""
    props: list[Unit] = []
    goal: list[Unit] = []
    for item in vb.fields(text):
        found = vb.goal_offset(item) if item.key == "goal" else None
        if found is None:
            props.append(_prop(item))
            continue
        parts = vb.blocks(found.markdown)
        lines = found.markdown.split("\n")
        head_last = found.header + parts[0].first - 1 if parts else found.last
        goal.append(Unit(found.header, head_last, "the goal", kind="goal", label="goal"))
        for part in parts:
            first = found.header + part.first
            depth = min(part.depth, vb.MAX_DEPTH)
            goal.append(
                Unit(
                    first,
                    found.header + part.last,
                    f"the {vb.BLOCK_NAMES.get(part.kind, 'block')} at line {first}",
                    kind=part.kind,
                    markdown=vb.block_markdown(lines, part),
                    depth=depth,
                )
            )
    return props, goal


# The texts of a run that take comments beside its definition, and the start
# of the ids of their units.
TEXT_PREFIX = {"plan": "P", "report": "R"}


def text_lines(text: str | None) -> list[str]:
    """The lines of a plan or a report, as the manager counts them."""
    return text.replace("\r\n", "\n").split("\n") if text else []


# -- the parts of a detail page ---------------------------------------------


def breakdown(usage: Usage) -> str:
    return (
        f"input {exact(usage.input)}, output {exact(usage.output)}, "
        f"cache read {exact(usage.cache_read)}, cache write {exact(usage.cache_write)}"
    )


def run_number(item: Detail) -> tuple[int, int] | None:
    """``(k, root)``: this run is run k of the lineage of the assignment
    ``root``. None when the run has no other run in its lineage."""
    ids = [run.id for run in item.lineage]
    if len(ids) < 2 or item.id not in ids:
        return None
    return ids.index(item.id) + 1, ids[0]


# The line on the page of a draft.
DRAFT_LINE = "Tell Joshua to start it, or leave comments first."


def start_phrase(assignment_id: int) -> str:
    """The words that make Joshua start exactly this draft."""
    return f"Tell Joshua: start assignment {int(assignment_id)}"
