"""The webhooks adapter: ``POST /v1/events``, the fleet-authed inbound for events.

External systems (Home Assistant, ops, cron jobs) post events here, not to core.
The route authenticates the caller against ``channels.webhooks.allowed_callers``,
lands any ``image_urls`` as attachments, and forwards a ``kind: event`` turn to
core. This replaces core's old ``/inject`` endpoint: channels now owns the auth
and the file download, so core never faces the public caller.

A ``verbatim`` event short-circuits: channels delivers the text straight to the
destination through the adapter (low latency, no model call) and still posts the
event to core so core records the ``inject`` transcript row. The order is deliver
first, then record.

The body is the v4 shape (``destination``, ``text``, ``verbatim``, ``image_urls``,
``event_type``, ``payload``, ``source``). A compatibility shim accepts the old
``/inject`` field names (``channel``, ``content``) for one release and logs their
use as deprecated. ``framing`` and ``handle`` are never read from the body; they
are set by channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import Attachment, Chat, EventOptions, Handle, TurnEvent
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger

from joshua_channels import destinations
from joshua_channels.deliver import (
    AttachmentError,
    AttachmentPipeline,
    ChannelsContext,
    map_attachments,
)
from joshua_channels.destinations import Resolved

logger = get_logger("channels.webhooks")

WEBHOOK_HANDLE_TYPE = "webhook"

# How long one image download may take before it is skipped.
IMAGE_TIMEOUT_S = 10.0

# The only URL schemes an image may use. Anything else (``file://``, ``data:``)
# is a 400, so the route never fetches a local or non-network resource.
_ALLOWED_SCHEMES = ("http", "https")

# Old ``/inject`` field name -> v4 field name. The shim maps these for one
# release; the v4 field wins when both are present.
_SHIM_FIELDS = {"channel": "destination", "content": "text"}

# Content type -> file extension for a downloaded image. An unknown type keeps
# the URL suffix or falls back to ``.bin``.
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heic",
    "image/bmp": ".bmp",
}


class EventValidationError(Exception):
    """The request body is malformed. The route answers 400 with the reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass
class DownloadOutcome:
    """The result of downloading one batch of ``image_urls``.

    ``stored`` names the files written to the inbox. ``skipped`` holds one human
    note per URL that could not be attached (too large, wrong status, or a
    transport error).
    """

    stored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def normalize_body(body: dict) -> dict:
    """Map the old ``/inject`` field names onto the v4 shape.

    Returns a new dict. Logs one deprecation line naming the old fields that were
    used. The v4 field wins when both an old and a new name are present.
    """
    normalized = dict(body)
    used_old: list[str] = []
    for old, new in _SHIM_FIELDS.items():
        if old not in normalized:
            continue
        value = normalized.pop(old)
        used_old.append(old)
        if new not in normalized:
            normalized[new] = value
    if used_old:
        logger.warning(
            {"message": "deprecated /inject field names on /v1/events", "fields": used_old}
        )
    return normalized


def validate_image_urls(raw: object) -> list[str]:
    """Return the URL list, or raise ``EventValidationError`` for a bad entry.

    Every entry must be an ``http`` or ``https`` string. A ``file://`` URL or a
    non-string entry is rejected, so the route never fetches a local resource.
    """
    if not isinstance(raw, list):
        raise EventValidationError("invalid_image_url", "image_urls must be a list")
    urls: list[str] = []
    for item in raw:
        if not isinstance(item, str) or urlparse(item).scheme.lower() not in _ALLOWED_SCHEMES:
            raise EventValidationError(
                "invalid_image_url", f"image_urls must be http(s) URLs: {item!r}"
            )
        urls.append(item)
    return urls


def _extension(content_type: str, url: str) -> str:
    """Pick a file extension from the sniffed content type, then the URL."""
    ext = _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())
    if ext:
        return ext
    suffix = Path(urlparse(url).path).suffix
    return suffix if suffix else ".bin"


async def download_image_urls(
    client: httpx.AsyncClient,
    urls: list[str],
    dest_dir: Path,
    *,
    max_bytes: int,
    timeout_s: float = IMAGE_TIMEOUT_S,
) -> DownloadOutcome:
    """Download each URL into ``dest_dir``, capped at ``max_bytes`` each.

    Streams every response so an oversize file never loads into memory. A file
    over the cap, a non-200 status, or a transport error is skipped with a note;
    the other images and the event still go through.
    """
    outcome = DownloadOutcome()
    for index, url in enumerate(urls):
        name, note = await _download_one(
            client, url, dest_dir, index, max_bytes=max_bytes, timeout_s=timeout_s
        )
        if name is not None:
            outcome.stored.append(name)
        if note is not None:
            outcome.skipped.append(note)
    return outcome


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: Path,
    index: int,
    *,
    max_bytes: int,
    timeout_s: float,
) -> tuple[str | None, str | None]:
    try:
        async with client.stream("GET", url, timeout=timeout_s, follow_redirects=True) as response:
            if response.status_code != 200:
                return None, f"{url}: HTTP {response.status_code}"
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return None, f"{url}: too large"
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    return None, f"{url}: too large"
            name = f"image-{index}{_extension(response.headers.get('content-type', ''), url)}"
            (dest_dir / name).write_bytes(buffer)
            return name, None
    except (httpx.HTTPError, OSError) as exc:
        logger.warning({"message": "image download failed", "url": url, "error": str(exc)})
        return None, f"{url}: {type(exc).__name__}"


def _skip_note(skipped: list[str]) -> str:
    """Return a short line to append to the event text for skipped images."""
    if not skipped:
        return ""
    count = len(skipped)
    plural = "s" if count != 1 else ""
    return f"\n\n(Note: {count} image{plural} could not be attached.)"


def _pipeline_target(
    ctx: ChannelsContext, resolved: Resolved | None
) -> tuple[str | None, str | None]:
    """Return the ``(person_id, group_id)`` the attachment pipeline stores under.

    A DM stores under its person; a group stores under the shared root; an event
    with no destination stores under the shared root as well.
    """
    if resolved is None:
        return None, None
    if resolved.kind == "group":
        group = ctx.settings.groups_by_chat(resolved.channel_type, resolved.chat_id)
        return None, (group.id if group is not None else resolved.title)
    return resolved.person_id, None


def _chat_for(resolved: Resolved | None) -> Chat:
    """Build the ``Chat`` for the turn. Core ignores it for events, but the shape
    requires one; a destination-less event carries an empty group chat."""
    if resolved is None:
        return Chat(kind="group", id="", title=None)
    return Chat(kind=resolved.kind, id=resolved.chat_id, title=resolved.title)


async def _land_images(
    ctx: ChannelsContext,
    urls: list[str],
    resolved: Resolved | None,
) -> tuple[list[Attachment], list[str]]:
    """Download the URLs and run the attachment pipeline. Returns
    ``(attachments, skipped_notes)``. A missing pipeline skips every file."""
    inbox = Path(ctx.data_dir) / "inbox" / uuid4().hex
    inbox.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient() as client:
        outcome = await download_image_urls(
            client, urls, inbox, max_bytes=ctx.settings.channels.limits.max_attachment_bytes
        )
    if not outcome.stored:
        return [], outcome.skipped
    pipeline: AttachmentPipeline | None = ctx.pipeline
    if pipeline is None:
        logger.warning(
            {"message": "no attachment pipeline; images skipped", "count": len(outcome.stored)}
        )
        return [], outcome.skipped
    person_id, group_id = _pipeline_target(ctx, resolved)
    attachments = await pipeline.process(inbox, person_id=person_id, group_id=group_id)
    return attachments, outcome.skipped


def _context(request: Request) -> ChannelsContext:
    return request.app.state.ctx


def build_events_router() -> APIRouter:
    """The events route: ``POST /v1/events``.

    The caller must be an identity in ``channels.webhooks.allowed_callers``. The
    route reads the live context from ``request.app.state.ctx`` at call time.
    """
    router = APIRouter()

    @router.post("/v1/events")
    async def post_event(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        allowed = ctx.settings.channels.webhooks.allowed_callers
        status, identity = authenticate(
            request.headers.get("authorization"), load_fleet_tokens(), allowed=allowed
        )
        if status == 401:
            return JSONResponse({"reason": "unauthorized"}, status_code=401)
        if status == 403:
            return JSONResponse({"reason": "forbidden"}, status_code=403)
        assert identity is not None

        try:
            raw = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            raw = None
        if not isinstance(raw, dict):
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        try:
            return await _handle_event(ctx, identity, normalize_body(raw))
        except EventValidationError as exc:
            return JSONResponse({"reason": exc.reason, "detail": exc.detail}, status_code=400)

    return router


async def _handle_event(ctx: ChannelsContext, identity: str, body: dict) -> JSONResponse:
    """Validate, land images, and forward one event. Raises
    ``EventValidationError`` for a bad body."""
    destination = str(body.get("destination", "")).strip()
    text_value = body.get("text")
    text = "" if text_value is None else str(text_value)
    event_type_value = body.get("event_type")
    event_type = str(event_type_value).strip() if event_type_value else None
    verbatim = bool(body.get("verbatim", False))
    payload = body.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise EventValidationError("invalid_payload", "payload must be an object")
    source = body.get("source")

    # A valid event is either a named event (event_type) or a channel-addressed
    # message (destination + text).
    if not event_type and not (destination and text):
        raise EventValidationError("invalid_event", "provide event_type, or destination and text")

    urls = validate_image_urls(body["image_urls"]) if body.get("image_urls") is not None else []

    resolved = (
        destinations.resolve(ctx.registry, ctx.settings, destination) if destination else None
    )
    if destination and resolved is None:
        return JSONResponse({"reason": "unknown_channel"}, status_code=404)
    if verbatim and resolved is None:
        raise EventValidationError("verbatim_needs_destination", "verbatim needs a destination")

    attachments, skipped = await _land_images(ctx, urls, resolved) if urls else ([], [])
    event_text = text + _skip_note(skipped)

    event = TurnEvent(
        channel=resolved.channel if resolved is not None else "",
        chat=_chat_for(resolved),
        handle=Handle(type=WEBHOOK_HANDLE_TYPE, id=identity),
        kind="event",
        text=event_text,
        attachments=attachments,
        event_type=event_type,
        payload=payload,
        options=EventOptions(verbatim=verbatim),
    )
    logger.info(
        {
            "message": "event received",
            "caller": identity,
            "destination": destination or None,
            "event_type": event_type,
            "verbatim": verbatim,
            "source": source,
            "attachments": len(attachments),
            "skipped": len(skipped),
        }
    )

    if verbatim:
        assert resolved is not None  # guarded above
        return await _deliver_verbatim(ctx, resolved, text, attachments, event)
    return await _forward(ctx, event)


async def _deliver_verbatim(
    ctx: ChannelsContext,
    resolved: Resolved,
    text: str,
    attachments: list[Attachment],
    event: TurnEvent,
) -> JSONResponse:
    """Deliver the text straight to the destination, then post the event to core
    so it records the transcript. Deliver first, then record."""
    try:
        files = map_attachments(ctx.data_dir, resolved, [a.path for a in attachments])
    except AttachmentError:
        files = []
    try:
        await resolved.adapter.send(resolved.chat_id, text, files)
    except Exception as exc:  # noqa: BLE001 — the adapter error becomes a 502
        logger.warning({"message": "verbatim delivery failed", "channel": resolved.channel})
        return JSONResponse({"reason": "send_failed", "detail": str(exc)}, status_code=502)
    if ctx.core_client is not None:
        try:
            await ctx.core_client.submit_turn(event)
        except Exception:  # noqa: BLE001 — the record is best effort; delivery already succeeded
            logger.warning({"message": "verbatim record failed", "channel": resolved.channel})
    return JSONResponse({"accepted": True, "verbatim": True}, status_code=202)


async def _forward(ctx: ChannelsContext, event: TurnEvent) -> JSONResponse:
    """Post the event to core and map its answer to the route response."""
    if ctx.core_client is None:
        logger.warning({"message": "no core client; event dropped", "channel": event.channel})
        return JSONResponse({"reason": "core_unavailable"}, status_code=503)
    try:
        ack = await ctx.core_client.submit_turn(event)
    except Exception as exc:  # noqa: BLE001 — a core outage is a 502 for the caller
        logger.warning({"message": "event submit failed", "channel": event.channel})
        return JSONResponse({"reason": "core_error", "detail": str(exc)}, status_code=502)
    if ack.status == 202:
        return JSONResponse({"accepted": True}, status_code=202)
    if ack.status == 200:
        return JSONResponse({"ignored": True}, status_code=200)
    return JSONResponse({"accepted": False, "reason": ack.reason}, status_code=ack.status or 502)
