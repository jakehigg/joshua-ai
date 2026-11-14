"""The terminal channel's inbound route.

``POST /v1/cli/turns/stream`` takes one message from a person at a shell, runs
the same guard every channel runs, and streams core's reply back. The terminal
is a channel, so an unknown person id is refused here and never reaches core.
The body is JSON (``person``, ``text``) or ``multipart/form-data`` (``person``,
``text``, and a repeated ``file``). A multipart turn writes every part into an
inbox directory and hands it to the attachment pipeline, the same pipeline every
other channel uses, so the turn carries stored paths and never bytes.

A turn does not stop when the person's connection does. The stream from core
is read by a task of its own, and the response only forwards what that task
reads. A caller that closes the connection (a phone that locks) stops the
forwarding, and the task goes on to the end of core's stream, so core never
sees a closed stream and the turn finishes. See ``_relay``.

``GET /v1/cli/outbox`` returns what core sent to this person while they were
away, and marks it read.

The ``/v1/cli/held`` routes carry the hold of a chat thread, which core keeps
(``/v1/holds``): the messages a person wrote while a turn of that thread was
still running. ``GET`` lists them, ``POST`` adds one, ``DELETE
/v1/cli/held/{id}`` takes one back, and ``POST /v1/cli/held/take`` empties the
hold and returns its messages joined as the text of one turn. Each call names
the person, and the optional ``workspace``, as a turn does.

A turn or a transcript read may name a ``workspace`` (the slug or the id). The
turn then goes to the channel ``workspace:<name>`` and the read gets the
thread of that workspace. Core finds the workspace for the person and refuses
one that the person does not own.

``GET /v1/cli/transcript`` reads the person's terminal conversation back from
core, a page at a time. It returns core's body and status as they are, and it
never logs a message.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.attachments import NO_CAPTION_TEXT
from joshua_shared.contracts import Attachment, Chat, Handle, TurnEvent
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger

from joshua_channels.adapters.cli import outbox_path
from joshua_channels.attachments import SKIP_TOO_LARGE
from joshua_channels.deliver import ChannelsContext

logger = get_logger("channels.cli")

CHANNEL_TYPE = "cli"
READ_SUFFIX = ".read"
ACKING_SUFFIX = ".acking"

# The form field names of a multipart turn.
PERSON_FIELD = "person"
TEXT_FIELD = "text"
FILE_FIELD = "file"
WORKSPACE_FIELD = "workspace"

# The channel type of a workspace thread. Core makes the channel with the workspace.
WORKSPACE_CHANNEL_TYPE = "workspace"

# One part is read in chunks of this size, so a part over the cap stops early.
PART_CHUNK_BYTES = 256 * 1024

# Characters allowed in an inbox filename. Everything else becomes ``_``.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@runtime_checkable
class _Part(Protocol):
    """One uploaded file part of a multipart body.

    The form parser gives a ``str`` for a text field and an upload object for a
    file part. This is the file part: a name the sender chose and the bytes.
    """

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass
class _Submission:
    """One parsed turn: the person, the text, and the files to ingest.

    ``files`` holds ``(inbox filename, bytes)`` pairs. The JSON body carries no
    file, so its list is always empty.
    """

    person_id: str
    text: str
    # The slug or the id of a workspace, or "" for the normal terminal chat.
    workspace: str = ""
    files: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def attachment_bytes(self) -> int:
        """The size of the largest file, the number the guard takes."""
        return max((len(data) for _, data in self.files), default=0)


def _workspace_ref(value: object) -> str:
    """The workspace of a request: its slug or id, or "" for none."""
    return value.strip() if isinstance(value, str) else ""


def _channel_id(person_id: str, workspace: str) -> str:
    """The channel of a turn: the person's terminal, or a workspace thread.

    Core resolves the name after ``workspace:``, as an id or as the slug of a
    workspace of that person, and refuses a turn on a workspace that the person
    does not own.
    """
    if workspace:
        return f"{WORKSPACE_CHANNEL_TYPE}:{workspace}"
    return f"{CHANNEL_TYPE}:{person_id}"


# The tasks that read core's stream. A task stays here until it ends, so it
# outlives the response of a caller that left.
_relays: set[asyncio.Task[None]] = set()


async def _relay(ctx: ChannelsContext, event: TurnEvent) -> AsyncIterator[str]:
    """Forward the frames of core's turn stream, as server-sent events.

    A task reads core into a queue. This generator ends when the caller leaves,
    and the task does not: it reads core to the end and drops what no one reads.
    A failure of the stream to core goes to the caller as the same exception,
    when the caller is still there.
    """
    queue: asyncio.Queue[tuple[str, dict] | BaseException | None] = asyncio.Queue()
    gone = False

    async def pump() -> None:
        try:
            async for frame in ctx.core_client.stream_turn(event):
                if not gone:
                    queue.put_nowait(frame)
        except Exception as exc:  # noqa: BLE001 — the caller gets it, or the log does
            if gone:
                logger.warning(
                    {"message": "cli turn: core stream failed after the caller left"}
                    | {"error": type(exc).__name__}
                )
            queue.put_nowait(exc)
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(pump(), name="cli-turn-relay")
    _relays.add(task)
    task.add_done_callback(_relays.discard)
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            name, data = item
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
    finally:
        if not task.done():
            gone = True
            logger.info({"message": "cli turn: the caller left; the turn goes on"})


def _context(request: Request) -> ChannelsContext:
    return request.app.state.ctx


def _is_multipart(request: Request) -> bool:
    """True when the caller sent a form instead of JSON."""
    content_type = request.headers.get("content-type", "")
    return content_type.split(";")[0].strip().lower() == "multipart/form-data"


def inbox_name(raw: str | None, index: int) -> str:
    """Return a safe inbox filename for one uploaded part.

    The sender's filename is kept, because the pipeline reports it back to the
    agent as ``original_name``, but every directory part is stripped and every
    unsafe character becomes ``_``, so a part named ``../../x.jpg`` cannot write
    outside the inbox directory. A part with no usable name gets ``file-<index>``.
    """
    base = posixpath.basename(str(raw or "").replace("\\", "/")).strip()
    name = _UNSAFE_CHARS.sub("_", base)[:96].strip("._")
    return name or f"file-{index}"


async def _read_part(part: _Part, *, max_bytes: int) -> bytes | None:
    """Read one part, or None when it is over ``max_bytes``."""
    buffer = bytearray()
    while True:
        chunk = await part.read(PART_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            return None
    return bytes(buffer)


def _field(form: Any, name: str) -> str:
    """Return a text field of the form, or ``""`` when a part came instead."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


async def _read_json(request: Request) -> _Submission | None:
    """Parse a JSON turn. Returns None for a body that is not an object."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
        body = None
    if not isinstance(body, dict):
        return None
    return _Submission(
        person_id=str(body.get("person") or "").strip(),
        text=str(body.get("text") or ""),
        workspace=_workspace_ref(body.get("workspace")),
    )


async def _read_multipart(request: Request, ctx: ChannelsContext) -> _Submission | None:
    """Parse a multipart turn, reading every ``file`` part into memory.

    A part over ``channels.limits.max_attachment_bytes`` is skipped and the turn
    still goes through, as on every other channel.
    """
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 — a bad form is a 400, not a 500
        return None
    max_bytes = ctx.settings.channels.limits.max_attachment_bytes
    try:
        submission = _Submission(
            person_id=_field(form, PERSON_FIELD).strip(),
            text=_field(form, TEXT_FIELD),
            workspace=_workspace_ref(_field(form, WORKSPACE_FIELD)),
        )
        for index, part in enumerate(form.getlist(FILE_FIELD)):
            if not isinstance(part, _Part):
                continue
            data = await _read_part(part, max_bytes=max_bytes)
            if data is None:
                logger.info(
                    {
                        "message": "cli attachment skipped",
                        "reason": SKIP_TOO_LARGE,
                        "name": inbox_name(part.filename, index),
                    }
                )
                continue
            taken = {held for held, _ in submission.files}
            submission.files.append((_unique_name(part.filename, index, taken), data))
    finally:
        await form.close()
    return submission


def _unique_name(raw: str | None, index: int, taken: set[str]) -> str:
    """Return the inbox filename of a part, kept apart from the parts before it."""
    name = inbox_name(raw, index)
    if name not in taken:
        return name
    stem, _, suffix = name.rpartition(".")
    return f"{stem}-{index}.{suffix}" if stem else f"{name}-{index}"


async def _land_files(
    ctx: ChannelsContext, files: list[tuple[str, bytes]], person_id: str
) -> list[Attachment]:
    """Write every part into a fresh inbox directory, then run the pipeline.

    The pipeline sniffs, normalizes, and stores each file under
    ``people/<person_id>/attachments/YYYY/MM/``, skips what is not an image, a
    PDF, or text, and deletes the inbox directory.
    """
    assert ctx.pipeline is not None
    inbox = Path(ctx.data_dir) / "inbox" / f"cli-{uuid4()}"
    inbox.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (inbox / name).write_bytes(data)
    return await ctx.pipeline.process(inbox, person_id=person_id, group_id=None)


def _allowed_callers(ctx: ChannelsContext) -> list[str]:
    """Identities that may speak for a person on the terminal channel."""
    return list(ctx.settings.channels.webhooks.allowed_callers)


def _auth(request: Request, ctx: ChannelsContext) -> JSONResponse | None:
    status, _ = authenticate(
        request.headers.get("authorization"), load_fleet_tokens(), allowed=_allowed_callers(ctx)
    )
    if status == 401:
        return JSONResponse({"reason": "unauthorized"}, status_code=401)
    if status == 403:
        return JSONResponse({"reason": "forbidden"}, status_code=403)
    return None


def _hold_thread(source: Mapping[str, Any], ctx: ChannelsContext) -> tuple[str, str] | JSONResponse:
    """The channel and the person of a hold call, or the answer that refuses it.

    ``source`` is the query of a read or the JSON body of a write. It names
    the person and, for a workspace thread, the ``workspace``.
    """
    person_id = str(source.get("person") or "").strip()
    if not person_id:
        return JSONResponse({"reason": "bad_request"}, status_code=400)
    if ctx.settings.person(person_id) is None:
        return JSONResponse({"reason": "unknown_person"}, status_code=403)
    if ctx.core_client is None:
        return JSONResponse({"reason": "core_unavailable"}, status_code=503)
    return _channel_id(person_id, _workspace_ref(source.get("workspace"))), person_id


async def _hold_call(
    ctx: ChannelsContext,
    method: str,
    path: str = "",
    *,
    params: Mapping[str, str] | None = None,
    json_body: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Pass one hold call on to core and answer with its status and body."""
    assert ctx.core_client is not None
    try:
        status, body = await ctx.core_client.call_hold(method, path, params=params, json=json_body)
    except httpx.HTTPError as exc:
        logger.warning({"message": "cli hold: core unavailable", "error": type(exc).__name__})
        return JSONResponse({"reason": "core_unavailable"}, status_code=503)
    return JSONResponse(body, status_code=status)


async def _hold_body(request: Request) -> dict[str, Any]:
    """The JSON body of a hold write. A body that is not an object is empty."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
        return {}
    return body if isinstance(body, dict) else {}


def build_cli_router() -> APIRouter:
    """The terminal channel routes."""
    router = APIRouter()

    @router.post("/v1/cli/turns/stream")
    async def post_cli_turn(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied

        if _is_multipart(request):
            submission = await _read_multipart(request, ctx)
        else:
            submission = await _read_json(request)
        if submission is None:
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        person_id = submission.person_id
        text = submission.text
        if not person_id or not (text.strip() or submission.files):
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        verdict = ctx.guard.check(
            channel_type=CHANNEL_TYPE,
            sender_handle=person_id,
            chat_id=person_id,
            chat_kind="dm",
            text_len=len(text),
            attachment_bytes=submission.attachment_bytes,
        )
        if not verdict.allowed:
            logger.warning(
                {"message": "cli turn refused", "person": person_id, "reason": verdict.reason}
            )
            return JSONResponse({"reason": verdict.reason or "refused"}, status_code=403)

        if submission.files and ctx.pipeline is None:
            logger.warning(
                {"message": "cli turn refused: no attachment pipeline", "person": person_id}
            )
            return JSONResponse({"reason": "pipeline_unavailable"}, status_code=503)

        if ctx.core_client is None:
            return JSONResponse({"reason": "core_unavailable"}, status_code=503)

        attachments: list[Attachment] = []
        if submission.files:
            attachments = await _land_files(ctx, submission.files, person_id)
        if submission.files and not text.strip():
            # The file is the message. Core's describer gives the turn its words.
            text = NO_CAPTION_TEXT

        event = TurnEvent(
            channel=_channel_id(person_id, submission.workspace),
            chat=Chat(kind="dm", id=person_id),
            handle=Handle(type=CHANNEL_TYPE, id=person_id),
            text=text,
            attachments=attachments,
            thread=None if submission.workspace else "main",
        )

        return StreamingResponse(_relay(ctx, event), media_type="text/event-stream")

    @router.get("/v1/cli/outbox")
    async def get_cli_outbox(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied

        person_id = str(request.query_params.get("person") or "").strip()
        if not person_id:
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        if ctx.settings.person(person_id) is None:
            return JSONResponse({"reason": "unknown_person"}, status_code=403)

        path = outbox_path(ctx.data_dir, person_id)
        ack = request.query_params.get("ack") == "true"
        source = path
        if ack:
            # Move the file aside before reading it. A line that core appends
            # after this point lands in a fresh outbox and waits for the next
            # call, instead of being deleted unread.
            source = path.with_suffix(path.suffix + ACKING_SUFFIX)
            try:
                path.rename(source)
            except FileNotFoundError:
                return JSONResponse({"messages": []})
        elif not path.exists():
            return JSONResponse({"messages": []})

        lines = [line for line in source.read_text().splitlines() if line.strip()]
        messages: list[dict[str, Any]] = []
        for line in lines:
            try:
                messages.append(json.loads(line))
            except ValueError:
                continue

        if ack:
            if lines:
                read = path.with_suffix(path.suffix + READ_SUFFIX)
                with read.open("a") as handle:
                    handle.write("\n".join(lines) + "\n")
            source.unlink()

        return JSONResponse({"messages": messages})

    @router.get("/v1/cli/transcript")
    async def get_cli_transcript(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied

        person_id = str(request.query_params.get("person") or "").strip()
        if not person_id:
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        if ctx.settings.person(person_id) is None:
            return JSONResponse({"reason": "unknown_person"}, status_code=403)
        if ctx.core_client is None:
            return JSONResponse({"reason": "core_unavailable"}, status_code=503)

        workspace = _workspace_ref(request.query_params.get("workspace"))
        params = {"channel": _channel_id(person_id, workspace), "person": person_id}
        if not workspace:
            params["thread"] = "main"  # the person's main thread, as for a turn
        for key in ("limit", "before"):
            value = request.query_params.get(key)
            if value is not None:
                params[key] = value
        try:
            status, body = await ctx.core_client.read_transcript(params)
        except httpx.HTTPError as exc:
            logger.warning(
                {
                    "message": "cli transcript: core unavailable",
                    "person": person_id,
                    "error": type(exc).__name__,
                }
            )
            return JSONResponse({"reason": "core_unavailable"}, status_code=503)
        return JSONResponse(body, status_code=status)

    @router.get("/v1/cli/held")
    async def get_cli_held(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied
        thread = _hold_thread(request.query_params, ctx)
        if isinstance(thread, JSONResponse):
            return thread
        channel, person_id = thread
        return await _hold_call(ctx, "GET", params={"channel": channel, "person": person_id})

    @router.post("/v1/cli/held")
    async def post_cli_held(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied
        body = await _hold_body(request)
        thread = _hold_thread(body, ctx)
        if isinstance(thread, JSONResponse):
            return thread
        channel, person_id = thread
        text = str(body.get("text") or "")
        if not text.strip():
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        return await _hold_call(
            ctx, "POST", json_body={"channel": channel, "person": person_id, "text": text}
        )

    @router.post("/v1/cli/held/take")
    async def post_cli_held_take(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied
        thread = _hold_thread(await _hold_body(request), ctx)
        if isinstance(thread, JSONResponse):
            return thread
        channel, person_id = thread
        return await _hold_call(
            ctx, "POST", "/take", json_body={"channel": channel, "person": person_id}
        )

    @router.delete("/v1/cli/held/{hold_id}")
    async def delete_cli_held(hold_id: str, request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied
        thread = _hold_thread(request.query_params, ctx)
        if isinstance(thread, JSONResponse):
            return thread
        channel, person_id = thread
        return await _hold_call(
            ctx, "DELETE", f"/{hold_id}", params={"channel": channel, "person": person_id}
        )

    return router
