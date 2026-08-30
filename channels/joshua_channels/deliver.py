"""Core's outbound API: ``POST /v1/deliver`` and ``GET /v1/channels/resolve``.

``/v1/deliver`` takes core's reply, resolves the target to an adapter, maps each
attachment to a data-volume path, and calls ``adapter.send``. Only the ``core``
identity may call either route. ``/v1/channels/resolve`` reports the concrete
channel and chat for a destination name or ref; core's events use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import Attachment, Chat, ResolveResponse
from joshua_shared.fleet_auth import require_identity
from joshua_shared.log import get_logger

from joshua_channels import destinations
from joshua_channels.core_client import CoreClient
from joshua_channels.destinations import Resolved
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry

logger = get_logger("channels.deliver")

DEFAULT_DATA_DIR = "/data"

_SHARED_ROOT = "shared"
_WIKI_ROOT = "wiki"


@runtime_checkable
class AttachmentPipeline(Protocol):
    """Turn downloaded files into ``Attachment`` records.

    The caller downloads files into ``inbox_dir`` and hands the directory over;
    the pipeline returns one ``Attachment`` per stored file, each ``path`` files-
    MCP relative (``attachments/YYYY/MM/<file>``).
    """

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]: ...


@dataclass
class ChannelsContext:
    """Live state the deliver and events routes read from ``request.app.state.ctx``."""

    settings: JoshuaConfig
    registry: AdapterRegistry
    data_dir: str = DEFAULT_DATA_DIR
    guard: Guard | None = None
    core_client: CoreClient | None = None
    pipeline: AttachmentPipeline | None = None


class AttachmentError(Exception):
    """An attachment path is unsafe or does not belong to the target chat."""


def map_attachments(data_dir: str, resolved: Resolved, paths: list[str]) -> list[Path]:
    """Map files-MCP relative paths to absolute data-volume paths.

    A ``wiki/…`` path maps to ``/data/wiki/…`` and a ``shared/…`` path to
    ``/data/shared/…``; both are readable by everyone. Any other path maps to
    the DM person's ``/data/people/<pid>/…``. A group chat, and a DM with no
    known person, accept only ``wiki/`` and ``shared/`` paths, so one person's
    files never reach another chat. Raises ``AttachmentError``
    for an unsafe or disallowed path.
    """
    root = Path(data_dir)
    result: list[Path] = []
    for raw in paths:
        rel = PurePosixPath(str(raw))
        parts = rel.parts
        if rel.is_absolute() or not parts or ".." in parts or "" in parts:
            raise AttachmentError(f"unsafe attachment path: {raw!r}")
        if parts[0] in (_SHARED_ROOT, _WIKI_ROOT):
            result.append(root / parts[0] / Path(*parts[1:]))
            continue
        if resolved.kind == "group":
            raise AttachmentError(f"group chat accepts only wiki/ and shared/ attachments: {raw!r}")
        if resolved.person_id is None:
            raise AttachmentError(f"no known person for attachment: {raw!r}")
        result.append(root / "people" / resolved.person_id / Path(*parts))
    return result


def _context(request: Request) -> ChannelsContext:
    return request.app.state.ctx


def build_router() -> APIRouter:
    """The deliver and resolve routes. Both are ``core``-only."""
    router = APIRouter()

    @router.post("/v1/deliver")
    async def deliver(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(["core"])),
    ):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        target = str(body.get("channel", "")).strip()
        text = str(body.get("text", ""))
        attachments = [str(a) for a in (body.get("attachments") or [])]

        ctx = _context(request)
        resolved = destinations.resolve(ctx.registry, ctx.settings, target)
        if resolved is None:
            logger.warning({"message": "deliver: unknown channel", "target": target})
            return JSONResponse({"reason": "unknown_channel"}, status_code=404)

        try:
            files = map_attachments(ctx.data_dir, resolved, attachments)
        except AttachmentError as exc:
            logger.warning({"message": "deliver: bad attachment", "target": target})
            return JSONResponse(
                {"reason": "invalid_attachment", "detail": str(exc)}, status_code=400
            )

        try:
            parts = await resolved.adapter.send(resolved.chat_id, text, files)
        except Exception as exc:  # noqa: BLE001 — the adapter error becomes a 502 for core
            logger.warning({"message": "deliver: adapter send failed", "target": resolved.channel})
            return JSONResponse({"reason": "send_failed", "detail": str(exc)}, status_code=502)

        return JSONResponse(
            {"delivered": True, "parts": parts if isinstance(parts, int) else 1}, status_code=200
        )

    @router.get("/v1/channels/refusals")
    async def recent_refusals(  # type: ignore[no-untyped-def]
        request: Request,
        limit: int = 5,
        _identity: str = Depends(require_identity(["core"])),
    ):
        """The handles the guard turned away, newest first.

        A refused message never reaches core, so the agent cannot name the sender
        of one. This is how a member can say "add the person who just messaged
        you". It returns the handle and the channel, never a message body.
        """
        ctx = _context(request)
        rows = ctx.guard.recent() if ctx.guard is not None else []
        return JSONResponse({"refusals": list(reversed(rows))[: max(1, min(limit, 20))]})

    @router.get("/v1/channels/resolve")
    async def resolve_channel(  # type: ignore[no-untyped-def]
        ref: str,
        request: Request,
        _identity: str = Depends(require_identity(["core"])),
    ):
        ctx = _context(request)
        resolved = destinations.resolve(ctx.registry, ctx.settings, ref)
        if resolved is None:
            return JSONResponse({"reason": "unknown_channel"}, status_code=404)
        # channel_id and chat_kind are flat aliases core's ChannelsResolver reads.
        return JSONResponse(
            ResolveResponse(
                channel=resolved.channel,
                chat=Chat(kind=resolved.kind, id=resolved.chat_id, title=resolved.title),
                channel_id=resolved.channel,
                chat_kind=resolved.kind,
            ).model_dump()
        )

    return router
