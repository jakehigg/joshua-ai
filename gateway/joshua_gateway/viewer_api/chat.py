"""The chat routes of the viewer: one turn, and the history of a conversation.

``POST /api/v1/chat/<person>/turns`` runs one turn on the channels container
and streams the answer back as server-sent events. The body is JSON
(``{"text", "spoken"}``), or ``multipart/form-data`` when the person attaches
one photo or one PDF: the fields ``text`` and ``file``. The file goes to a
temporary file as it arrives and is streamed on to channels, so the whole
body is never in memory. ``TURN_MAX_BYTES`` caps the text and the file
together, and the 413 answer names the limit. The write checks the ``Origin``
header, as every write route does.

The frames from channels pass through unchanged: ``accepted`` (core has the
turn, and it will finish), ``status`` (``queued`` or ``running``), ``delta``,
``done``, and ``error``. A browser that leaves does not stop the turn: a task
reads the stream from channels to its end whether or not anyone reads the
response (see ``Relay``), and the log line of the turn says ``client_gone``.
A refusal before the stream starts is a JSON answer with a mapped
status. A failure after the stream started is one ``error`` frame, then the
end of the stream. While channels is silent, a comment line goes out every
``SSE_KEEPALIVE_S`` seconds, so a proxy with a read timeout keeps the stream
open through a long turn.

A turn body and a history query take an optional ``workspace``: a slug, an id,
or a two-segment ref (``shared/<slug>`` for a shared workspace,
``<owner>/<slug>`` for the workspace of a person). Without it, the turn runs in
the main thread of the person.

``GET /api/v1/chat/<person>/history`` gives one page of the main thread of the
person (or of the workspace), from the channels transcript, and ``pending``:
the turn in flight (``{turnId, state, since, text}``, ``state`` is ``queued`` or
``running``), or ``null``. A message with files has ``attachments``, each
``{name, mime, size, url}``; ``url`` is a ``/api/v1/files`` address of the file,
and it is missing when the person may not read the file through the viewer. ``in`` is the
person, ``out`` is Joshua, and any other direction is ``system``. A person row
that starts with the speech marker loses the marker and gets
``"spoken": true``. A person row of a shared workspace carries ``who``, the
name of the person who wrote it.

The hold is the messages the person wrote while a turn of the thread was
still running. It lives in core, so a reload or a closed tab never loses one.
``GET /api/v1/chat/<person>/held`` lists them (``{held: [{id, text, at}]}``),
``POST`` adds one (``{"text", "workspace"}``), ``DELETE
/api/v1/chat/<person>/held/<id>`` takes one back, and ``POST
/api/v1/chat/<person>/held/take`` empties the hold and answers with ``text``:
every held message in order, one per line, which the page then sends as one
turn.

The route serves any person id that ``joshua.yaml`` knows. With no
``CHANNELS_URL`` or no viewer token, every route answers 503.

Every refusal is ``{"error": "<message>"}``, with a ``reason`` when the
refusal came from channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.log import get_logger
from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from joshua_gateway import viewer_channels
from joshua_gateway.viewer_api.edit import _origin_ok
from joshua_gateway.viewer_api.files import file_url
from joshua_gateway.viewer_channels import (
    Attachment,
    ChannelsClient,
    ChannelsError,
    ChannelsUnavailable,
    TurnStream,
)
from joshua_gateway.viewer_multipart import (
    BodyTooLarge,
    MultipartError,
    Part,
    boundary_of,
    read_form,
)

logger = get_logger("viewer.chat")

MAX_TEXT_CHARS = 8000
LOST = "the connection to Joshua was lost"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
KEEPALIVE = b": keepalive\n\n"
# The multipart field that carries the photo or the PDF.
FILE_FIELD = "file"
DEFAULT_FILE_TYPE = "application/octet-stream"
BAD_BODY = "the body must be JSON or multipart/form-data"

UNKNOWN_PERSON = "Joshua does not know this person"
NOT_CONFIGURED = "chat is not configured on the Joshua side"
UNREACHABLE = "Joshua is unreachable"
CROSS_SITE = "the request comes from another site"

PERSON_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# The ref of a workspace: a slug, an id, or two segments
# (``shared/<slug>`` for a shared workspace, ``<owner>/<slug>`` for the
# workspace of a person). Core finds it for the person.
_REF_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
WORKSPACE_REF = re.compile(rf"^{_REF_SEGMENT}(/{_REF_SEGMENT})?$")
BAD_WORKSPACE = "workspace: not a workspace slug or id"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ROLES = {"in": "person", "out": "joshua"}

# reason from channels -> (status for the browser, message)
REASONS: dict[str, tuple[int, str]] = {
    "unknown_sender": (404, UNKNOWN_PERSON),
    "unknown_person": (404, UNKNOWN_PERSON),
    "rate_limited": (429, "too many messages; wait a minute and try again"),
    "unknown_hold": (404, "that message is not waiting any more"),
    "hold_full": (429, "too many messages are waiting; wait for Joshua to finish one"),
    "core_unavailable": (503, "Joshua is not available right now"),
    "pipeline_unavailable": (
        503,
        "Joshua cannot read a file right now; send the message without it",
    ),
}

# The marker that asks Joshua for a short spoken version of the answer. It is
# part of the person's text in the transcript, so the history route removes it.
MARKER = "[speak-brief]"
# The one character that follows the marker. A CRLF line end counts as one.
_SEPARATORS = ("\r\n", "\n", " ")


def unmark(text: str) -> tuple[str, bool]:
    """Remove a leading marker and the one space or newline after it.

    Return the text and whether a marker was there. A marker that is not at
    the start, or that has no space or newline after it, stays in the text.
    """
    if not text.startswith(MARKER):
        return text, False
    rest = text[len(MARKER) :]
    for separator in _SEPARATORS:
        if rest.startswith(separator):
            return rest[len(separator) :], True
    return text, False


def _json(body: dict[str, Any], status: int) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def channels_error(exc: ChannelsError) -> JSONResponse:
    """Return the answer for a channels refusal. The answer never holds the token."""
    if exc.reason in REASONS:
        status, message = REASONS[exc.reason]
    elif exc.status in (401, 403):
        status, message = 502, NOT_CONFIGURED
    elif exc.status == 503:
        status, message = 503, "Joshua is not available right now"
    else:
        status, message = 502, "Joshua refused the request"
    return _json({"error": message, "reason": exc.reason}, status)


def unreachable() -> JSONResponse:
    return _json({"error": UNREACHABLE}, 503)


def _known(person: str) -> bool:
    """True when ``person`` has the shape of an id and ``joshua.yaml`` knows it."""
    return bool(PERSON_ID.fullmatch(person)) and config.load().person(person) is not None


class TurnIn(BaseModel):
    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS)
    ]
    # "brief" asks Joshua for a short spoken version. Outgoing spoken turns
    # are not part of this route, so the value is read and ignored.
    spoken: Any = None
    # The slug or the id of a workspace, or "" for the main chat.
    workspace: str = ""


@dataclass
class Turn:
    """One turn as the body gave it. ``upload`` holds the file, when there is one."""

    text: str
    spoken: Any = None
    upload: Part | None = None
    workspace: str = ""


def _workspace_of(value: Any) -> str:
    """The workspace of a body, or "" for none. A bad value is refused."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not WORKSPACE_REF.fullmatch(value.strip()):
        raise _Refused(422, BAD_WORKSPACE)
    return value.strip()


class _Refused(Exception):
    """The body cannot be used; answer the person with this status and message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _size(limit: int) -> str:
    """The limit as a person reads it."""
    if limit >= 1024 * 1024:
        return f"{limit / (1024 * 1024):g} MB"
    return f"{limit / 1024:g} KB"


def too_large(limit: int) -> str:
    return f"the message with its file is too large; the limit is {_size(limit)}"


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the whole body. Refuse it as soon as it passes ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _Refused(413, too_large(limit))
        chunks.append(chunk)
    return b"".join(chunks)


def _checked_text(raw: str, has_file: bool) -> str:
    """The text as channels gets it. A text is required unless a file comes with it."""
    text = raw.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise _Refused(422, f"text: must be at most {MAX_TEXT_CHARS} characters")
    if not text and not has_file:
        raise _Refused(422, "text: must not be empty")
    return text


def _validation_message(exc: ValidationError) -> str:
    """The first error as ``<field>: <message>``."""
    errors = exc.errors()
    if not errors:
        return "invalid request"
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg"))


def _json_turn(raw: bytes) -> Turn:
    """The turn of a JSON body, checked by ``TurnIn``."""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise _Refused(422, BAD_BODY) from None
    if not isinstance(payload, dict):
        raise _Refused(422, BAD_BODY)
    try:
        turn = TurnIn.model_validate(payload)
    except ValidationError as exc:
        raise _Refused(422, _validation_message(exc)) from None
    return Turn(turn.text, turn.spoken, workspace=_workspace_of(payload.get("workspace")))


def _form_turn(parts: list[Part]) -> Turn:
    """The turn of a multipart body: one ``file`` part at most, beside the fields."""
    files = [part for part in parts if part.name == FILE_FIELD]
    fields = {part.name: part.text() for part in parts if part.name != FILE_FIELD}
    upload = files[0] if len(files) == 1 else None
    try:
        if len(files) > 1:
            raise _Refused(422, f"{FILE_FIELD}: one file per message")
        if upload is not None and upload.size == 0:
            raise _Refused(422, f"{FILE_FIELD}: the file is empty")
        text = _checked_text(fields.get("text", ""), has_file=upload is not None)
        workspace = _workspace_of(fields.get("workspace"))
    except _Refused:
        for part in parts:
            part.close()
        raise
    for part in parts:
        if part is not upload:
            part.close()
    return Turn(text, fields.get("spoken"), upload, workspace)


async def read_turn(request: Request, limit: int) -> Turn:
    """Read the turn from the body, JSON or multipart. Raise ``_Refused`` on a bad body."""
    content_type = request.headers.get("content-type", "")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _Refused(413, too_large(limit))
    if boundary_of(content_type) is None:
        return _json_turn(await _read_capped(request, limit))
    try:
        parts = await read_form(request.stream(), content_type, limit)
    except BodyTooLarge:
        raise _Refused(413, too_large(limit)) from None
    except MultipartError:
        raise _Refused(422, BAD_BODY) from None
    return _form_turn(parts)


def sse_frame(event: str, data: Any) -> bytes:
    """Encode one server-sent event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _log_turn(
    person: str,
    status: str,
    started: float,
    chars_in: int,
    file_bytes: int = 0,
    client_gone: bool = False,
) -> None:
    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        {
            "message": "turn",
            "person": person,
            "status": status,
            "duration_ms": duration_ms,
            "chars_in": chars_in,
            "file_bytes": file_bytes,
            "client_gone": client_gone,
        }
    )


# The relays that are still reading channels. A relay stays here until its
# turn ends, so it outlives the response of a browser that left.
_relays: set[asyncio.Task[None]] = set()


class Relay:
    """One turn's stream from channels, read by a task of its own.

    The task reads the stream into a queue and finishes the turn (it closes the
    stream and the client, and logs the real status). ``frames`` only forwards
    what the task has read. A browser that leaves ends ``frames`` and nothing
    else: the task goes on to the end of the stream, so channels and core
    never see a closed connection and the turn finishes. ``status`` is ``ok``
    after a ``done`` frame, ``error`` after an ``error`` frame, ``lost`` when
    the connection to channels failed, and ``incomplete`` when the stream ended
    with neither.
    """

    def __init__(
        self,
        stream: TurnStream,
        client: ChannelsClient,
        person: str,
        started: float,
        chars_in: int,
        file_bytes: int = 0,
    ) -> None:
        self.status = "incomplete"
        self.client_gone = False
        self._queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self._stream = stream
        self._client = client
        self._log = (person, started, chars_in, file_bytes)
        self._person = person
        self.task = asyncio.create_task(self._pump(), name="viewer-turn-relay")
        _relays.add(self.task)
        self.task.add_done_callback(_relays.discard)

    async def _pump(self) -> None:
        try:
            async for event, data in self._stream.events():
                if event == "done":
                    self.status = "ok"
                elif event == "error":
                    self.status = "error"
                self._queue.put_nowait((event, data))
        except httpx.HTTPError:
            self.status = "lost"
            self._queue.put_nowait(("error", {"message": LOST}))
        finally:
            self._queue.put_nowait(None)
            with contextlib.suppress(Exception):
                await self._stream.aclose()
            with contextlib.suppress(Exception):
                await self._client.aclose()
            person, started, chars_in, file_bytes = self._log
            _log_turn(person, self.status, started, chars_in, file_bytes, self.client_gone)

    async def frames(self) -> AsyncIterator[bytes]:
        """The frames as bytes, with a keepalive comment while the stream is silent."""
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), self._client.settings.sse_keepalive_s
                    )
                except TimeoutError:
                    yield KEEPALIVE
                    continue
                if item is None:
                    return
                yield sse_frame(*item)
        finally:
            if not self.task.done():
                self.client_gone = True
                logger.info({"message": "turn", "person": self._person, "status": "client_gone"})


async def turns(request: Request, person: Person) -> JSONResponse | StreamingResponse:
    """Run one turn for ``{person}`` and stream the answer as server-sent events."""
    if not _origin_ok(request):
        return _json({"error": CROSS_SITE, "reason": "cross_site"}, 403)
    who = request.path_params["person"]
    if not _known(who):
        return _json({"error": UNKNOWN_PERSON}, 404)
    client = viewer_channels.from_env()
    if client is None:
        return _json({"error": NOT_CONFIGURED}, 503)
    started = time.monotonic()
    try:
        turn = await read_turn(request, client.settings.turn_max_bytes)
    except _Refused as exc:
        await client.aclose()
        return _json({"error": exc.message}, exc.status)
    chars_in = len(turn.text)
    upload = turn.upload
    file_bytes = upload.size if upload is not None else 0
    attachment: Attachment | None = None
    if upload is not None:
        attachment = (upload.filename, upload.open(), upload.content_type or DEFAULT_FILE_TYPE)
    try:
        stream = await client.open_turn(who, turn.text, attachment, workspace=turn.workspace)
    except ChannelsError as exc:
        _log_turn(who, f"refused:{exc.reason}", started, chars_in, file_bytes)
        await client.aclose()
        return channels_error(exc)
    except ChannelsUnavailable:
        _log_turn(who, "unreachable", started, chars_in, file_bytes)
        await client.aclose()
        return unreachable()
    finally:
        # The body has gone out by now, so the temporary file can go.
        if upload is not None:
            upload.close()
    relay = Relay(stream, client, who, started, chars_in, file_bytes)
    return StreamingResponse(
        relay.frames(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def map_held(value: Any) -> list[dict[str, Any]]:
    """The held messages of a thread, each ``{id, text, at}``, in order."""
    held: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        held.append(
            {
                "id": item["id"],
                "text": item["text"] if isinstance(item.get("text"), str) else "",
                "at": item["at"] if isinstance(item.get("at"), str) else "",
            }
        )
    return held


async def _hold_call(
    request: Request,
    method: str,
    *,
    path: str = "",
    text: str | None = None,
    workspace: str = "",
) -> dict[str, Any] | JSONResponse:
    """Call one hold route of channels for the person of the path.

    The answer is the body of channels, or the refusal to send the browser.
    """
    who = request.path_params["person"]
    if not _known(who):
        return _json({"error": UNKNOWN_PERSON}, 404)
    client = viewer_channels.from_env()
    if client is None:
        return _json({"error": NOT_CONFIGURED}, 503)
    try:
        return await client.held(method, who, path=path, text=text, workspace=workspace)
    except ChannelsError as exc:
        return channels_error(exc)
    except ChannelsUnavailable:
        return unreachable()
    finally:
        await client.aclose()


async def _hold_body(request: Request) -> tuple[str, str] | JSONResponse:
    """The ``text`` and the ``workspace`` of a hold write, or the refusal."""
    try:
        payload = json.loads(await request.body() or b"{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return _json({"error": BAD_BODY}, 422)
    try:
        workspace = _workspace_of(payload.get("workspace"))
    except _Refused as exc:
        return _json({"error": exc.message}, exc.status)
    raw = payload.get("text")
    return (raw if isinstance(raw, str) else ""), workspace


async def held(request: Request, person: Person) -> JSONResponse:
    """The messages this thread holds, oldest first."""
    try:
        workspace = _workspace_of(request.query_params.get("workspace"))
    except _Refused as exc:
        return _json({"error": exc.message}, exc.status)
    answer = await _hold_call(request, "GET", workspace=workspace)
    if isinstance(answer, JSONResponse):
        return answer
    return _json({"held": map_held(answer.get("held"))}, 200)


async def hold(request: Request, person: Person) -> JSONResponse:
    """Hold one message, written while a turn of this thread still runs."""
    if not _origin_ok(request):
        return _json({"error": CROSS_SITE, "reason": "cross_site"}, 403)
    body = await _hold_body(request)
    if isinstance(body, JSONResponse):
        return body
    text, workspace = body
    try:
        checked = _checked_text(text, has_file=False)
    except _Refused as exc:
        return _json({"error": exc.message}, exc.status)
    answer = await _hold_call(request, "POST", text=checked, workspace=workspace)
    if isinstance(answer, JSONResponse):
        return answer
    return _json({"held": map_held(answer.get("held"))}, 200)


async def take_held(request: Request, person: Person) -> JSONResponse:
    """Empty the hold and answer with its messages as the text of one turn."""
    if not _origin_ok(request):
        return _json({"error": CROSS_SITE, "reason": "cross_site"}, 403)
    body = await _hold_body(request)
    if isinstance(body, JSONResponse):
        return body
    answer = await _hold_call(request, "POST", path="/take", workspace=body[1])
    if isinstance(answer, JSONResponse):
        return answer
    taken = map_held(answer.get("held"))
    # Core joins the messages, one per line; the join here is only a fallback.
    text = (
        answer["text"]
        if isinstance(answer.get("text"), str)
        else "\n".join(m["text"] for m in taken)
    )
    return _json({"text": text, "held": taken}, 200)


async def drop_held(request: Request, person: Person) -> JSONResponse:
    """Take one held message back, before it goes."""
    if not _origin_ok(request):
        return _json({"error": CROSS_SITE, "reason": "cross_site"}, 403)
    try:
        workspace = _workspace_of(request.query_params.get("workspace"))
    except _Refused as exc:
        return _json({"error": exc.message}, exc.status)
    answer = await _hold_call(
        request, "DELETE", path=f"/{request.path_params['id']}", workspace=workspace
    )
    if isinstance(answer, JSONResponse):
        return answer
    return _json({"held": map_held(answer.get("held"))}, 200)


def _bad(message: str) -> JSONResponse:
    return _json({"error": message}, 422)


def _int(value: str | None, name: str, low: int, high: int | None) -> int | None | JSONResponse:
    """Parse an optional integer query value in ``[low, high]``; a bad value is a 422 answer."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except ValueError:
        return _bad(f"{name}: must be an integer")
    if number < low or (high is not None and number > high):
        bounds = f"from {low} to {high}" if high is not None else f"{low} or more"
        return _bad(f"{name}: must be {bounds}")
    return number


def attachment_url(path: str, person: str) -> str | None:
    """The viewer address of a file of the data volume, or None.

    The files route serves the wiki, the shared files, and the attachments of
    the person who signed in. A file of another person gets no address, and a
    path with ``..`` in it never gets one.
    """
    parts = path.split("/")
    if ".." in parts or "" in parts:
        return None
    if len(parts) >= 3 and parts[0] == "wiki":
        return file_url("wiki", "/".join(parts[1:]))
    if len(parts) >= 3 and parts[0] == "shared":
        return file_url("shared", "/".join(parts[1:]))
    if (
        len(parts) >= 4
        and parts[0] == "people"
        and parts[1] == person
        and parts[2] == "attachments"
    ):
        return file_url("attachments", "/".join(parts[3:]))
    return None


def map_attachments(value: Any, person: str) -> list[dict[str, Any]]:
    """The files of a person's message, as ``{name, mime, size, url}``."""
    files: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = item["path"]
        entry: dict[str, Any] = {
            "name": item["name"] if isinstance(item.get("name"), str) else path.rsplit("/", 1)[-1],
            "mime": item["mime"] if isinstance(item.get("mime"), str) else DEFAULT_FILE_TYPE,
        }
        if isinstance(item.get("size"), int):
            entry["size"] = item["size"]
        url = attachment_url(path, person)
        if url is not None:
            entry["url"] = url
        files.append(entry)
    return files


def map_pending(value: Any) -> dict[str, Any] | None:
    """The turn in flight, or None. The text is the person's message, or ``""``."""
    if not isinstance(value, dict) or not isinstance(value.get("turn_id"), str):
        return None
    return {
        "turnId": value["turn_id"],
        "state": "queued" if value.get("state") == "queued" else "running",
        "since": value.get("since") if isinstance(value.get("since"), str) else "",
        "text": value.get("text") if isinstance(value.get("text"), str) else "",
    }


def map_message(row: Any, person: str = "") -> dict[str, Any] | None:
    """Map one transcript row to the browser shape. A row with no integer id is dropped."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), int):
        return None
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    role = ROLES.get(str(row.get("direction")), "system")
    text = row.get("text") if isinstance(row.get("text"), str) else ""
    spoken = False
    if role == "person":
        text, spoken = unmark(text)
    message: dict[str, Any] = {
        "id": row["id"],
        "role": role,
        "text": text,
        "status": "failed" if row.get("status") == "failed" else "ok",
        "at": row.get("at") if isinstance(row.get("at"), str) else "",
    }
    if spoken:
        message["spoken"] = True
    # The thread of a shared workspace names who wrote a person row.
    if role == "person" and isinstance(meta.get("speaker"), str) and meta["speaker"]:
        message["who"] = meta["speaker"]
    if isinstance(meta.get("turn_id"), str):
        message["turnId"] = meta["turn_id"]
    attachments = map_attachments(meta.get("attachments"), person)
    if attachments:
        message["attachments"] = attachments
    tools = meta.get("tools")
    if isinstance(tools, list):
        message["tools"] = [name for name in tools if isinstance(name, str)]
    return message


def map_page(page: dict[str, Any], person: str = "") -> dict[str, Any]:
    rows = page.get("messages")
    mapped = [map_message(row, person) for row in rows] if isinstance(rows, list) else []
    next_before = page.get("next_before")
    return {
        "messages": [message for message in mapped if message is not None],
        "nextBefore": next_before if isinstance(next_before, int) else None,
        "pending": map_pending(page.get("pending")),
    }


async def history(request: Request, person: Person) -> JSONResponse:
    """One page of the conversation, oldest first, and the cursor of the page before."""
    who = request.path_params["person"]
    if not _known(who):
        return _json({"error": UNKNOWN_PERSON}, 404)
    # The values are checked after the person, so an unknown person is 404
    # whatever the query holds. No bad value reaches channels.
    size = _int(request.query_params.get("limit"), "limit", 1, MAX_LIMIT)
    if isinstance(size, JSONResponse):
        return size
    cursor = _int(request.query_params.get("before"), "before", 1, None)
    if isinstance(cursor, JSONResponse):
        return cursor
    try:
        workspace = _workspace_of(request.query_params.get("workspace"))
    except _Refused as exc:
        return _json({"error": exc.message}, exc.status)
    client = viewer_channels.from_env()
    if client is None:
        return _json({"error": NOT_CONFIGURED}, 503)
    try:
        page = await client.transcript(
            who,
            limit=DEFAULT_LIMIT if size is None else size,
            before=cursor,
            workspace=workspace,
        )
    except ChannelsError as exc:
        answer = channels_error(exc)
        logger.info(
            {
                "message": "history refused",
                "person": who,
                "status": answer.status_code,
                "reason": exc.reason,
                "count": 0,
            }
        )
        return answer
    except ChannelsUnavailable:
        logger.info({"message": "history unreachable", "person": who, "status": 503, "count": 0})
        return unreachable()
    finally:
        await client.aclose()
    result = map_page(page, who)
    logger.info(
        {"message": "history", "person": who, "status": 200, "count": len(result["messages"])}
    )
    return _json(result, 200)
