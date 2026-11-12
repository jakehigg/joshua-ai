"""The chat routes of the viewer: one turn, and the history of a conversation.

``POST /api/v1/chat/<person>/turns`` runs one turn on the channels container
and streams the answer back as server-sent events. The body is JSON
(``{"text", "spoken"}``), or ``multipart/form-data`` when the person attaches
one photo or one PDF: the fields ``text`` and ``file``. The file goes to a
temporary file as it arrives and is streamed on to channels, so the whole
body is never in memory. ``TURN_MAX_BYTES`` caps the text and the file
together, and the 413 answer names the limit. The write checks the ``Origin``
header, as every write route does.

The frames from channels pass through unchanged: ``delta``, ``done``, and
``error``. A refusal before the stream starts is a JSON answer with a mapped
status. A failure after the stream started is one ``error`` frame, then the
end of the stream. While channels is silent, a comment line goes out every
``SSE_KEEPALIVE_S`` seconds, so a proxy with a read timeout keeps the stream
open through a long turn.

``GET /api/v1/chat/<person>/history`` gives one page of the ``cli:``
conversation of the person, from the channels transcript. ``in`` is the
person, ``out`` is Joshua, and any other direction is ``system``. A person row
that starts with the speech marker loses the marker and gets
``"spoken": true``.

The route serves any person id that ``joshua.yaml`` knows. With no
``CHANNELS_URL`` or no viewer token, both routes answer 503.

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

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ROLES = {"in": "person", "out": "joshua"}

# reason from channels -> (status for the browser, message)
REASONS: dict[str, tuple[int, str]] = {
    "unknown_sender": (404, UNKNOWN_PERSON),
    "unknown_person": (404, UNKNOWN_PERSON),
    "rate_limited": (429, "too many messages; wait a minute and try again"),
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


@dataclass
class Turn:
    """One turn as the body gave it. ``upload`` holds the file, when there is one."""

    text: str
    spoken: Any = None
    upload: Part | None = None


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
    return Turn(turn.text, turn.spoken)


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
    except _Refused:
        for part in parts:
            part.close()
        raise
    for part in parts:
        if part is not upload:
            part.close()
    return Turn(text, fields.get("spoken"), upload)


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
        }
    )


async def _relay(
    stream: TurnStream,
    client: ChannelsClient,
    person: str,
    started: float,
    chars_in: int,
    file_bytes: int = 0,
) -> AsyncIterator[bytes]:
    """Relay the frames of ``stream``, with a keepalive comment while it is silent.

    A task reads the stream into a queue. The relay waits on the queue with
    a timeout: a timeout sends the keepalive, and never touches the stream.
    """
    queue: asyncio.Queue[tuple[str, Any] | httpx.HTTPError | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for frame in stream.events():
                await queue.put(frame)
        except httpx.HTTPError as exc:
            await queue.put(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    status = "ok"
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), client.settings.sse_keepalive_s)
            except TimeoutError:
                yield KEEPALIVE
                continue
            if item is None:
                break
            if isinstance(item, httpx.HTTPError):
                status = "lost"
                yield sse_frame("error", {"message": LOST})
                break
            event, data = item
            if event == "error":
                status = "error"
            yield sse_frame(event, data)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await stream.aclose()
        await client.aclose()
        _log_turn(person, status, started, chars_in, file_bytes)


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
        stream = await client.open_turn(who, turn.text, attachment)
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
    return StreamingResponse(
        _relay(stream, client, who, started, chars_in, file_bytes),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


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


def map_message(row: Any) -> dict[str, Any] | None:
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
    if isinstance(meta.get("turn_id"), str):
        message["turnId"] = meta["turn_id"]
    tools = meta.get("tools")
    if isinstance(tools, list):
        message["tools"] = [name for name in tools if isinstance(name, str)]
    return message


def map_page(page: dict[str, Any]) -> dict[str, Any]:
    rows = page.get("messages")
    mapped = [map_message(row) for row in rows] if isinstance(rows, list) else []
    next_before = page.get("next_before")
    return {
        "messages": [message for message in mapped if message is not None],
        "nextBefore": next_before if isinstance(next_before, int) else None,
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
    client = viewer_channels.from_env()
    if client is None:
        return _json({"error": NOT_CONFIGURED}, 503)
    try:
        page = await client.transcript(
            who, limit=DEFAULT_LIMIT if size is None else size, before=cursor
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
    result = map_page(page)
    logger.info(
        {"message": "history", "person": who, "status": 200, "count": len(result["messages"])}
    )
    return _json(result, 200)
