"""``GET /v1/transcript``: read one conversation back, a page at a time.

A chat client calls this route to show the messages of a conversation. The
callers are ``channels`` and ``laptop``. The query names the channel and, for a
conversation with one person, the person. The route never creates a
conversation: an unknown one answers ``200`` with no messages.

Each message carries only the ``meta`` keys in ``SAFE_META_KEYS``:

- ``turn_id``: the turn the row belongs to.
- ``tools``: the names of the tools the turn used.
- ``call_count``: how many tool calls the turn made.
- ``duration_ms``: how long the turn took.
- ``first_output_ms``: the time to the first streamed text.

Every other key is dropped, for example the token usage, the cost, a speaker's
handle, and the paths that the turn wrote. The route never logs a message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from joshua_shared.fleet_auth import require_identity

from joshua_core.store.repo import TRANSCRIPT_PAGE_MAX

TRANSCRIPT_PATH = "/v1/transcript"
DEFAULT_LIMIT = 50

# The meta keys a chat client may see. A key that is not here is dropped.
SAFE_META_KEYS = ("turn_id", "tools", "call_count", "duration_ms", "first_output_ms")


def safe_meta(meta: Any) -> dict[str, Any]:
    """Return only the ``SAFE_META_KEYS`` of a transcript row's meta."""
    if not isinstance(meta, dict):
        return {}
    return {key: meta[key] for key in SAFE_META_KEYS if key in meta}


def _iso_utc(value: Any) -> str | None:
    """Render a timestamp as ISO 8601 in UTC. A naive time is taken as UTC."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def message(row: dict[str, Any]) -> dict[str, Any]:
    """Render one transcript row as a message of the reply."""
    return {
        "id": row["id"],
        "direction": row["direction"],
        "text": row["content"],
        "status": row.get("status") or "ok",
        "at": _iso_utc(row.get("created_at")),
        "meta": safe_meta(row.get("meta")),
    }


def build_transcript_router() -> APIRouter:
    """The transcript router. ``channels`` and ``laptop`` may call it."""
    router = APIRouter()

    @router.get(TRANSCRIPT_PATH)
    async def get_transcript(  # type: ignore[no-untyped-def]
        request: Request,
        channel: str | None = None,
        person: str | None = None,
        limit: int = Query(DEFAULT_LIMIT, ge=1, le=TRANSCRIPT_PAGE_MAX),
        before: int | None = Query(None, ge=1),
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        channel_id = (channel or "").strip()
        if not channel_id:
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        person_id = (person or "").strip() or None

        repo = request.app.state.ctx.repo
        conversation = await repo.find_conversation(channel_id, person_id)
        if conversation is None:
            return JSONResponse({"conversation_id": None, "messages": [], "next_before": None})

        rows, next_before = await repo.transcript_page(conversation.id, limit, before)
        return JSONResponse(
            {
                "conversation_id": str(conversation.id),
                "messages": [message(r) for r in rows],
                "next_before": next_before,
            }
        )

    return router
