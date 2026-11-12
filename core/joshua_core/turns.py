"""The channels → core inbound API: ``POST /v1/turns`` and ``/v1/turns/stream``.

One implementation for every channel. A ``TurnEvent`` resolves to a
(person, channel, conversation) triple, then either runs in the background and
delivers the reply through channels (``/v1/turns``) or runs inline and streams
the reply back over SSE (``/v1/turns/stream``).

``TurnService`` holds the resolution logic, the idempotency cache, and the
background-turn set. The two routes are thin wrappers that authenticate the
caller and call it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.contracts import TurnEvent
from joshua_shared.fleet_auth import require_identity
from joshua_shared.log import get_logger

from joshua_core.delivery import Deliverer, is_ignore, workspace_delivery_target
from joshua_core.engine.types import FAILED_REPLY
from joshua_core.engine.types import Attachment as EngineAttachment
from joshua_core.events import EventService
from joshua_core.store.models import Channel, Conversation, Person
from joshua_core.store.repo import WORKSPACE_CHANNEL_TYPE, Repo

logger = get_logger("turns")

# The prefix of a workspace channel id.
WORKSPACE_PREFIX = f"{WORKSPACE_CHANNEL_TYPE}:"

# Idempotency window: a message_id seen inside this many seconds is a duplicate.
SEEN_TTL_SECONDS = 900

# The reply core delivers when a turn raises. No detail leaks to the channel.
TURN_FAILED = FAILED_REPLY


class SeenCache:
    """Remember message ids for a short window so a retried post never re-runs.

    ``check_and_add`` marks an id seen and returns whether it was already there.
    Expired ids are purged on each call, so the cache stays small.
    """

    def __init__(self, ttl_seconds: int = SEEN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    def _purge(self, now: float) -> None:
        cutoff = now - self._ttl
        for key in [k for k, seen_at in self._seen.items() if seen_at < cutoff]:
            del self._seen[key]

    def check_and_add(self, key: str) -> bool:
        now = monotonic()
        self._purge(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


@dataclass
class Resolution:
    """The routing result for one turn."""

    channel: Channel
    conversation: Conversation
    person: Person | None
    is_group: bool
    speaker: str | None


class TurnService:
    """Resolve, run, and deliver turns. One instance per process."""

    def __init__(
        self,
        *,
        repo: Repo,
        settings: Any,
        manager: Any,
        deliverer: Deliverer,
        events: EventService | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._manager = manager
        self._deliverer = deliverer
        self._events = events
        self._seen = SeenCache()
        self._background: set[asyncio.Task[None]] = set()

    # --- resolution --------------------------------------------------------

    async def resolve(self, event: TurnEvent) -> Resolution | None:
        """Turn an event into a (person, channel, conversation) triple.

        Returns None for an unknown sender — the caller answers 403. A ``cli``
        handle resolves straight to the person whose id equals the handle id. A
        group message from an unknown handle still runs when the group is known,
        attributed to the group's default person (or nobody).
        """
        handle = event.handle
        if handle is None:
            return None
        is_group = event.chat.kind == "group"

        if handle.type == "cli":
            person = await self._repo.get_person(handle.id)
        else:
            person = await self._repo.get_person_by_handle(handle.type, handle.id)

        if person is None:
            if not is_group:
                return None
            group = self._settings.groups_by_chat(handle.type, event.chat.id)
            if group is None:
                return None
            default_person_id = getattr(group, "default_person", None)
            if default_person_id:
                person = await self._repo.get_person(default_person_id)

        if event.channel.startswith(WORKSPACE_PREFIX):
            return await self._resolve_workspace(event, person)

        channel = await self._repo.get_channel(event.channel)
        if channel is None:
            await self._repo.upsert_channel(
                event.channel,
                handle.type,
                display_name=event.chat.title,
                session_mode="shared" if is_group else "per_person",
                default_person_id=(person.id if (not is_group and person) else None),
            )
            channel = await self._repo.get_channel(event.channel)
            assert channel is not None

        conversation_person = None if is_group else (person.id if person else None)
        conversation = await self._repo.get_or_create_conversation(channel.id, conversation_person)

        speaker = None
        if is_group:
            speaker = person.display_name if person else handle.id

        return Resolution(
            channel=channel,
            conversation=conversation,
            person=person,
            is_group=is_group,
            speaker=speaker,
        )

    async def _resolve_workspace(
        self, event: TurnEvent, person: Person | None
    ) -> Resolution | None:
        """Resolve a turn on ``workspace:<id or slug>``.

        The workspace must exist, and its owner must be the person of the turn.
        Any other turn is refused: None, and the caller answers 403. A turn
        never creates a ``workspace:`` channel. The ``workspaces`` routes
        create it with the workspace.
        """
        if person is None or event.chat.kind == "group":
            return None
        ref = event.channel[len(WORKSPACE_PREFIX) :]
        workspace = await self._repo.find_workspace(person.id, ref)
        if workspace is None:
            logger.warning({"message": "workspace turn refused", "reason": "no such workspace"})
            return None
        channel = await self._repo.get_channel(workspace.channel_id)
        if channel is None:
            return None
        conversation = await self._repo.get_or_create_conversation(channel.id, person.id)
        return Resolution(
            channel=channel, conversation=conversation, person=person, is_group=False, speaker=None
        )

    # --- run helpers -------------------------------------------------------

    def _engine_attachments(self, event: TurnEvent) -> list[EngineAttachment]:
        return [
            EngineAttachment(
                path=a.path, mime=a.mime, name=a.name or "", original_name=a.original_name
            )
            for a in event.attachments
        ]

    async def _run(self, resolution: Resolution, event: TurnEvent, on_delta: Any = None) -> Any:
        person_id = resolution.person.id if resolution.person else None
        return await self._manager.run_turn(
            resolution.channel,
            resolution.conversation,
            event.text,
            on_delta=on_delta,
            framing=event.framing,
            speaker=resolution.speaker,
            person_id=person_id,
            attachments=self._engine_attachments(event),
        )

    # --- POST /v1/turns ----------------------------------------------------

    def _at_capacity(self) -> bool:
        return len(self._background) >= self._settings.core.pool_max * 2

    async def accept(self, event: TurnEvent) -> JSONResponse:
        """Handle ``POST /v1/turns``: idempotency, resolve, 202 + background run.

        A ``kind == "event"`` turn skips sender resolution and goes to the event
        router, which maps a named event to a skill or delivers a channel-addressed
        event.
        """
        if event.kind == "event":
            if self._events is None:
                return JSONResponse({"reason": "events_unavailable"}, status_code=501)
            return await self._events.handle(event)

        if event.message_id and self._seen.check_and_add(event.message_id):
            return JSONResponse({"accepted": False, "reason": "duplicate"}, status_code=200)

        resolution = await self.resolve(event)
        if resolution is None:
            logger.warning(
                {
                    "message": "unknown sender",
                    "channel": event.channel,
                    "handle_type": event.handle.type if event.handle else None,
                }
            )
            return JSONResponse({"reason": "unknown_sender"}, status_code=403)

        if self._at_capacity():
            logger.warning({"message": "turn rejected: at capacity", "channel": event.channel})
            return JSONResponse({"accepted": False, "reason": "overloaded"}, status_code=429)

        turn_id = "t-" + resolution.conversation.id.replace("-", "")[:12]
        task = asyncio.create_task(self._run_and_deliver(resolution, event))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return JSONResponse({"accepted": True, "turn_id": turn_id}, status_code=202)

    async def _run_and_deliver(self, resolution: Resolution, event: TurnEvent) -> None:
        try:
            result = await self._run(resolution, event)
        except Exception as exc:  # noqa: BLE001 — any turn failure still answers the channel
            logger.error({"message": "turn failed", "channel": event.channel, "error": str(exc)})
            target = workspace_delivery_target(
                channel_type=resolution.channel.channel_type, target=event.channel
            )
            if target is not None:
                await self._deliverer.deliver(target, TURN_FAILED)
            return
        text = result.text or ""
        target = workspace_delivery_target(
            channel_type=resolution.channel.channel_type, target=event.channel
        )
        if text and not is_ignore(text) and target is not None:
            await self._deliverer.deliver(target, text)

    # --- POST /v1/turns/stream ---------------------------------------------

    async def stream(self, event: TurnEvent) -> StreamingResponse | JSONResponse:
        """Handle ``POST /v1/turns/stream``: resolve, run inline, stream SSE."""
        resolution = await self.resolve(event)
        if resolution is None:
            return JSONResponse({"reason": "unknown_sender"}, status_code=403)

        turn_id = "t-" + resolution.conversation.id.replace("-", "")[:12]
        return StreamingResponse(
            self._sse(resolution, event, turn_id),
            media_type="text/event-stream",
        )

    async def _sse(self, resolution: Resolution, event: TurnEvent, turn_id: str) -> Any:
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

        async def on_delta(chunk: str) -> None:
            await queue.put(("delta", {"text": chunk}))

        async def run() -> None:
            try:
                result = await self._run(resolution, event, on_delta=on_delta)
                await queue.put(
                    (
                        "done",
                        {"turn_id": turn_id, "text": result.text or "", "tools": result.tools_used},
                    )
                )
            except Exception as exc:  # noqa: BLE001 — surface the failure as an SSE error
                logger.error(
                    {"message": "stream turn failed", "channel": event.channel, "error": str(exc)}
                )
                await queue.put(("error", {"message": str(exc)}))
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                name, data = item
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
        finally:
            await task

    async def drain(self) -> None:
        """Await every in-flight background turn. Called on shutdown."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
        if self._events is not None:
            await self._events.drain()


# --- routes ----------------------------------------------------------------


def _service(request: Request) -> TurnService:
    return request.app.state.ctx.turns


def build_router() -> APIRouter:
    """The turns router. ``/v1/turns`` is channels-only; ``/v1/turns/stream``
    also accepts ``laptop`` for a ``cli`` handle."""
    router = APIRouter()

    @router.post("/v1/turns")
    async def post_turn(  # type: ignore[no-untyped-def]
        event: TurnEvent,
        request: Request,
        _identity: str = Depends(require_identity(["channels"])),
    ):
        return await _service(request).accept(event)

    @router.post("/v1/turns/stream")
    async def post_turn_stream(  # type: ignore[no-untyped-def]
        event: TurnEvent,
        request: Request,
        identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        if identity == "laptop" and (event.handle is None or event.handle.type != "cli"):
            return JSONResponse({"reason": "forbidden"}, status_code=403)
        return await _service(request).stream(event)

    return router
