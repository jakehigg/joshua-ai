"""Shared API shapes for the routes between containers.

Channels normalizes every platform message into a ``TurnEvent`` and posts it to
core. Both containers import these models, so the wire shape has one definition.
Core never trusts ``framing`` from user input; only the scheduler and ops set it.

``SearchRequest`` and ``SearchResponse`` are the shapes of core's
``POST /v1/memory/search``, which the viewer and an addon call.
``UsageResponse`` is the shape of core's ``GET /v1/usage``, which the viewer
calls for its usage page. ``WebSearchRequest`` and ``WebSearchResponse`` are
the shapes of the gateway ``POST /v1/search``, which core calls for the
internet agent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HandleType = Literal["telegram", "imessage", "webhook", "voice", "cli"]
ChatKind = Literal["dm", "group"]
TurnKind = Literal["message", "event"]

# The document kinds that ``POST /v1/memory/search`` can return: the pages of
# the wiki, the journal, and the profiles. Every reader of the wiki can read
# them. A person's own attachments (kind ``people``) and the skill rows are
# never in this list.
SearchKind = Literal["wiki", "journal", "profile"]
SEARCH_KINDS: tuple[SearchKind, ...] = ("wiki", "journal", "profile")
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 25
SEARCH_MAX_QUERY_CHARS = 1000


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Handle(_Model):
    """The verified sender, stamped by channels. ``id`` is the platform handle."""

    type: HandleType
    id: str


class Chat(_Model):
    """The conversation surface. ``kind`` picks per-person or shared (group)."""

    kind: ChatKind
    id: str
    title: str | None = None


class Attachment(_Model):
    """One inbound file. ``path`` is files-MCP relative: ``attachments/YYYY/MM/<file>``.

    ``name`` is the stored filename. ``original_name`` is what the platform called
    the file, so the agent can name it back to the person.
    """

    path: str
    mime: str
    name: str | None = None
    original_name: str | None = None


class EventOptions(_Model):
    """Options for a ``kind == "event"`` turn.

    ``verbatim`` makes core deliver a channel-addressed event's ``text`` straight
    to the channel with no model call.
    """

    verbatim: bool = False


class TurnEvent(_Model):
    """One normalized turn from any channel.

    ``handle`` is None only for ``kind == "event"``. ``message_id`` is the
    platform id and the idempotency key. ``event_type`` and ``payload`` carry an
    event (``kind == "event"``); core routes those.
    """

    channel: str
    chat: Chat
    handle: Handle | None = None
    kind: TurnKind = "message"
    text: str = ""
    attachments: list[Attachment] = []
    reply_to: str | None = None
    message_id: str | None = None
    event_type: str | None = None
    payload: dict | None = None
    options: EventOptions | None = None
    framing: str | None = None


class TurnAck(_Model):
    """Core's answer to a submitted turn, as the channels client records it.

    ``accepted`` is True only for a 202. ``status`` is the HTTP status the client
    saw. ``turn_id`` is set on a 202. ``reason`` names why a turn was not accepted
    (``unknown_sender``, ``overloaded``, ``duplicate``, or core's own reason).
    """

    accepted: bool
    status: int
    turn_id: str | None = None
    reason: str | None = None


class DeliverRequest(_Model):
    """Core's outbound reply. Channels resolves ``channel`` to an adapter.

    ``channel`` is a channel id, a logical ref, or a destination name.
    ``attachments`` are files-MCP relative paths (``attachments/YYYY/MM/<file>``
    or ``shared/…``); channels maps each to a data-volume path.
    """

    channel: str
    text: str = ""
    attachments: list[str] = []


class ResolveResponse(_Model):
    """The concrete channel and chat for a destination name or ref.

    ``channel_id`` and ``chat_kind`` are flat aliases of ``channel`` and
    ``chat.kind`` that core's channels resolver reads.
    """

    channel: str
    chat: Chat
    channel_id: str
    chat_kind: ChatKind


class SearchRequest(_Model):
    """One search of the memory by meaning. ``kinds`` empty or absent means all
    of ``SEARCH_KINDS``."""

    query: str = Field(min_length=1, max_length=SEARCH_MAX_QUERY_CHARS)
    limit: int = Field(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT)
    kinds: list[SearchKind] | None = None


class SearchHit(_Model):
    """One passage. ``path`` is relative to the data volume, for example
    ``wiki/journal/2026/09/26/garden.md``. ``date`` is the journal day or a
    frontmatter date, as ``YYYY-MM-DD``."""

    path: str
    title: str
    heading: str
    text: str
    kind: SearchKind
    score: float
    date: str | None = None


class SearchResponse(_Model):
    results: list[SearchHit]


# The window of ``GET /v1/usage``, in days.
USAGE_DEFAULT_DAYS = 30
USAGE_MAX_DAYS = 366


class UsageCounts(_Model):
    """The calls and the tokens of one group or one worker. ``total`` is
    input, output, cache read, and cache write, added. ``models`` is the
    sorted list of the distinct models of its calls, and empty when no call
    named one."""

    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0
    models: list[str] = []


class UsageGroup(UsageCounts):
    """The counts of one group, and of each worker in it. The map is empty
    for a group whose rows name no worker."""

    workers: dict[str, UsageCounts] = {}


class UsageDay(_Model):
    """One calendar day in the instance timezone, as ``YYYY-MM-DD``, and the
    groups that used tokens on it."""

    date: str
    groups: dict[str, UsageGroup]


class UsageResponse(_Model):
    """The tokens of each group by day. ``rows`` is newest first and holds
    only the days with usage, and a day holds only the groups with usage that
    day. ``groups`` names each feature, also one with no usage, in the order
    of the page, then any other group in the window, sorted. ``totals`` holds
    one entry for each name of ``groups``."""

    timezone: str
    days: int
    groups: list[str]
    rows: list[UsageDay]
    totals: dict[str, UsageGroup]


# The path of the gateway snapshot route. ``{name}`` is a configured snapshot.
SNAPSHOT_PATH = "/v1/snapshots/{name}"


class SnapshotResponse(_Model):
    """The answer of the gateway ``POST /v1/snapshots/{name}``.

    ``text`` joins the text parts of the tool result with a newline.
    ``is_error`` is the tool's own error flag. ``ok`` is true when the call ran
    and the tool did not set ``is_error``. ``fetched_at`` is the UTC time of
    the call, in ISO 8601.
    """

    ok: bool
    text: str
    is_error: bool
    fetched_at: str


# The path of the gateway web search route, and the limits of its shapes.
WEB_SEARCH_PATH = "/v1/search"
WEB_SEARCH_MAX_QUERY_CHARS = 500
WEB_SEARCH_MAX_TITLE_CHARS = 300
WEB_SEARCH_MAX_SNIPPET_CHARS = 1000
WEB_SEARCH_MAX_URL_CHARS = 2000


class WebSearchRequest(_Model):
    """The body of the gateway ``POST /v1/search``: one query, and nothing else.

    The gateway reads the SearXNG instance, its parameters, and its key from
    ``internet.search``, so the caller cannot choose them.
    """

    query: str = Field(min_length=1, max_length=WEB_SEARCH_MAX_QUERY_CHARS)


class WebSearchResult(_Model):
    """One result of a web search. ``url`` is always ``http`` or ``https``."""

    title: str = Field(default="", max_length=WEB_SEARCH_MAX_TITLE_CHARS)
    url: str = Field(max_length=WEB_SEARCH_MAX_URL_CHARS)
    snippet: str = Field(default="", max_length=WEB_SEARCH_MAX_SNIPPET_CHARS)


class WebSearchResponse(_Model):
    """The answer of the gateway ``POST /v1/search``, at most
    ``internet.search.max_results`` results."""

    results: list[WebSearchResult]
