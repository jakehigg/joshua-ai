"""The terminal channel's inbound route.

``POST /v1/cli/turns/stream`` takes one message from a person at a shell, runs
the same guard every channel runs, and streams core's reply back. The terminal
is a channel, so an unknown person id is refused here and never reaches core.

``GET /v1/cli/outbox`` returns what core sent to this person while they were
away, and marks it read.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.contracts import Chat, Handle, TurnEvent
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger

from joshua_channels.adapters.cli import outbox_path
from joshua_channels.deliver import ChannelsContext

logger = get_logger("channels.cli")

CHANNEL_TYPE = "cli"
READ_SUFFIX = ".read"
ACKING_SUFFIX = ".acking"


def _context(request: Request) -> ChannelsContext:
    return request.app.state.ctx


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


def build_cli_router() -> APIRouter:
    """The terminal channel routes."""
    router = APIRouter()

    @router.post("/v1/cli/turns/stream")
    async def post_cli_turn(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        denied = _auth(request, ctx)
        if denied is not None:
            return denied

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        person_id = str(body.get("person") or "").strip()
        text = str(body.get("text") or "")
        if not person_id or not text.strip():
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        verdict = ctx.guard.check(
            channel_type=CHANNEL_TYPE,
            sender_handle=person_id,
            chat_id=person_id,
            chat_kind="dm",
            text_len=len(text),
            attachment_bytes=0,
        )
        if not verdict.allowed:
            logger.warning(
                {"message": "cli turn refused", "person": person_id, "reason": verdict.reason}
            )
            return JSONResponse({"reason": verdict.reason or "refused"}, status_code=403)

        if ctx.core_client is None:
            return JSONResponse({"reason": "core_unavailable"}, status_code=503)

        event = TurnEvent(
            channel=f"{CHANNEL_TYPE}:{person_id}",
            chat=Chat(kind="dm", id=person_id),
            handle=Handle(type=CHANNEL_TYPE, id=person_id),
            text=text,
        )

        async def stream():  # type: ignore[no-untyped-def]
            async for name, data in ctx.core_client.stream_turn(event):
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

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

    return router
