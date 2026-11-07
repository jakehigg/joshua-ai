"""The peer routes on channels.

- ``POST /v1/peers/send`` (``gateway``): put one item in the outbox and send
  it to a peer.
- ``POST /v1/peers/show`` (``gateway``): deliver one stored item, title and
  body as received, to a person. The reply never holds the body.
- ``POST /v1/peers/notice`` (``core``): send the fixed-text notice for one
  stored item.

Each route answers 404 ``not_found`` when ``peers`` is empty. No model is
involved in any of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared import layout
from joshua_shared.config import JoshuaConfig, Peer
from joshua_shared.fleet_auth import require_identity
from joshua_shared.log import get_logger
from joshua_shared.peers import AckEnvelope, Attachment, EnvelopeError, ItemEnvelope, parse_envelope

from joshua_channels import destinations
from joshua_channels.deliver import ChannelsContext
from joshua_channels.destinations import Resolved
from joshua_channels.peers.outbox import OutboxExists
from joshua_channels.peers.service import PeersService
from joshua_channels.peers.store import load_item, sha256_file

logger = get_logger("channels.peers.routes")

# The channel types a person is reached on for a peer item, in this order. The
# first one where the person has a direct chat is used.
DEFAULT_CHANNEL_ORDER = ("telegram", "imessage", "cli")

NOTICE_TEMPLATE = "New {kind} from {peer}: {title}"
NOTICE_TITLE_CHARS = 80

_DETAIL_CHARS = 300


class AttachmentRefused(Exception):
    """An ``attachments_from`` entry is outside the allowed folders or does not match."""


def notice_text(kind: str, peer_name: str, title: str) -> str:
    """The fixed notice. The title loses its line breaks and is cut to 80 characters."""
    clean = " ".join(title.split())[:NOTICE_TITLE_CHARS]
    return NOTICE_TEMPLATE.format(kind=kind, peer=peer_name, title=clean)


def person_destination(ctx: ChannelsContext, person_id: str) -> Resolved | None:
    """The person's direct chat on the first channel in ``DEFAULT_CHANNEL_ORDER``."""
    for channel_type in DEFAULT_CHANNEL_ORDER:
        ref = f"{channel_type}:dm:{person_id}"
        resolved = destinations.resolve(ctx.registry, ctx.settings, ref)
        if resolved is not None:
            return resolved
    return None


def check_attachments_from(
    data_dir: Path | str, envelope: ItemEnvelope, paths: list[str]
) -> list[tuple[Attachment, Path]]:
    """Match each path to one attachment of the envelope. Raises ``AttachmentRefused``.

    A path must be absolute and resolve to a file inside ``wiki/`` or
    ``people/``, with no dot folder on the way. Its sha256 and size must equal
    those of one attachment entry, and each entry needs exactly one path.
    """
    wanted = {a.sha256: a for a in envelope.body.attachments}
    if len(paths) != len(wanted):
        raise AttachmentRefused("attachments_from must name one file for each attachment")
    roots = [layout.wiki_root(data_dir).resolve(), layout.people_root(data_dir).resolve()]
    matched: list[tuple[Attachment, Path]] = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise AttachmentRefused("attachment path must be absolute")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AttachmentRefused("attachment path does not exist") from exc
        base = next((root for root in roots if resolved.is_relative_to(root)), None)
        if base is None or layout.is_hidden(resolved, base):
            raise AttachmentRefused("attachment path is outside wiki/ and people/")
        if not resolved.is_file():
            raise AttachmentRefused("attachment path is not a file")
        digest = sha256_file(resolved)
        attachment = wanted.pop(digest, None)
        if attachment is None:
            raise AttachmentRefused("attachment sha256 does not match the envelope")
        if resolved.stat().st_size != attachment.size:
            raise AttachmentRefused("attachment size does not match the envelope")
        matched.append((attachment, resolved))
    return matched


def _peer(cfg: JoshuaConfig, peer_id: str) -> Peer | None:
    return next((p for p in cfg.peers if p.id == peer_id), None)


def _error(reason: str, status: int, detail: str | None = None) -> JSONResponse:
    body: dict[str, str] = {"reason": reason}
    if detail:
        body["detail"] = detail[:_DETAIL_CHARS]
    return JSONResponse(body, status_code=status)


async def _body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
        return None
    return body if isinstance(body, dict) else None


def _service(request: Request) -> tuple[ChannelsContext, PeersService | None]:
    ctx: ChannelsContext = request.app.state.ctx
    return ctx, getattr(ctx, "peers", None)


def build_peers_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/peers/send")
    async def send(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(["gateway"])),
    ):
        ctx, service = _service(request)
        if service is None:
            return _error("not_found", 404)
        body = await _body(request)
        if body is None or not isinstance(body.get("envelope"), dict):
            return _error("bad_request", 400)
        attachments_from = body.get("attachments_from") or []
        if not isinstance(attachments_from, list) or not all(
            isinstance(p, str) for p in attachments_from
        ):
            return _error("bad_request", 400, "attachments_from must be a list of paths")
        peer = _peer(ctx.settings, str(body.get("peer_id", "")))
        if peer is None:
            return _error("unknown_peer", 404)
        try:
            envelope = parse_envelope(json.dumps(body["envelope"]).encode("utf-8"))
        except EnvelopeError as exc:
            return _error("invalid_envelope", 400, str(exc))
        if isinstance(envelope, AckEnvelope):
            return _error("kind_not_item", 400)
        if ctx.settings.person(envelope.sender_person) is None:
            return _error("unknown_person", 400)
        if envelope.expired(service.clock()):
            return _error("expired", 400)
        try:
            files = check_attachments_from(ctx.data_dir, envelope, attachments_from)
        except AttachmentRefused as exc:
            logger.warning({"message": "peer send: attachment refused", "id": envelope.id})
            return _error("invalid_attachment", 400, str(exc))
        if service.transport is None:
            return _error("telegram_unavailable", 503)
        try:
            service.outbox.enqueue(peer.id, peer.telegram_bot_id, envelope, files)
        except OutboxExists:
            return _error("duplicate", 409)
        service.send_soon(envelope.id)
        return JSONResponse({"id": envelope.id}, status_code=202)

    @router.post("/v1/peers/show")
    async def show(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(["gateway"])),
    ):
        ctx, service = _service(request)
        if service is None:
            return _error("not_found", 404)
        body = await _body(request)
        if body is None:
            return _error("bad_request", 400)
        item_id = str(body.get("id", ""))
        person = ctx.settings.person(str(body.get("person_id", "")))
        if person is None:
            return _error("unknown_person", 404)
        item = load_item(ctx.data_dir, item_id)
        if item is None:
            return _error("unknown_item", 404)
        recipient = item.meta.get("recipient")
        if recipient != person.id and not (recipient is None and person.role == "member"):
            logger.warning({"message": "peer show refused", "id": item_id, "person": person.id})
            return _error("not_allowed", 403)
        resolved = person_destination(ctx, person.id)
        if resolved is None:
            return _error("no_destination", 404)
        text = f"{item.envelope.body.title}\n\n{item.envelope.body.markdown}"
        try:
            await resolved.adapter.send(resolved.chat_id, text, item.attachment_paths())
        except Exception as exc:  # noqa: BLE001 — the adapter error becomes a 502
            logger.warning(
                {"message": "peer show: send failed", "id": item_id, "error": type(exc).__name__}
            )
            return _error("send_failed", 502)
        logger.info({"message": "peer item shown", "id": item_id, "person": person.id})
        return JSONResponse({"ok": True})

    @router.post("/v1/peers/notice")
    async def notice(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(["core"])),
    ):
        ctx, service = _service(request)
        if service is None:
            return _error("not_found", 404)
        body = await _body(request)
        if body is None:
            return _error("bad_request", 400)
        item_id = str(body.get("id", ""))
        item = load_item(ctx.data_dir, item_id)
        if item is None:
            return _error("unknown_item", 404)
        peer_id = str(item.meta.get("peer_id", ""))
        peer = _peer(ctx.settings, peer_id)
        if peer is not None and peer.on_receive == "silent":
            return JSONResponse({"ok": True, "sent": 0})
        recipient = item.meta.get("recipient")
        if isinstance(recipient, str) and ctx.settings.person(recipient) is not None:
            people = [recipient]
        else:
            people = [p.id for p in ctx.settings.people if p.role == "member"]
        text = notice_text(
            item.envelope.kind, peer.name if peer else peer_id, item.envelope.body.title
        )
        sent = 0
        for person_id in people:
            resolved = person_destination(ctx, person_id)
            if resolved is None:
                continue
            try:
                await resolved.adapter.send(resolved.chat_id, text, [])
                sent += 1
            except Exception as exc:  # noqa: BLE001 — one failed notice must not stop the rest
                logger.warning(
                    {"message": "peer notice failed", "id": item_id, "error": type(exc).__name__}
                )
        logger.info({"message": "peer notice", "id": item_id, "sent": sent})
        return JSONResponse({"ok": True, "sent": sent})

    return router
