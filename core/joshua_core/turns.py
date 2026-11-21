"""The channels → core inbound API: ``POST /v1/turns`` and ``/v1/turns/stream``.

One implementation for every channel. A ``TurnEvent`` resolves to a
(person, channel, conversation) triple, then either runs in the background and
delivers the reply through channels (``/v1/turns``) or runs inline and streams
the reply back over SSE (``/v1/turns/stream``).

``TurnService`` holds the resolution logic, the idempotency cache, and the
background-turn set. The two routes are thin wrappers that authenticate the
caller and call it.

A streamed turn runs in a task of its own, and the HTTP stream only reads
what that task puts on a queue. A client that closes the stream (a phone that
locks, a tab that closes, a proxy that times out) stops the reading and nothing
else: the turn goes on, and its reply lands in the transcript like the reply of
any other turn. The turn is cancelled only when the process shuts down. The
stream tells the client what happens, in these events: ``accepted`` (core has
the turn and it will finish), ``status`` (``queued`` while another turn of the
conversation runs, then ``running``), ``delta``, ``done``, and ``error``.

Nothing on the client's side of the queue can fail the turn: ``put`` swallows
what it cannot deliver, and the engine does the same with the hooks it is
given. Each end of a streamed turn logs its own line with the ``turn_id``:
``client left a running turn`` and ``turn finished after the client left`` when
a client goes, ``stream turn cancelled`` on shutdown, and ``stream turn
failed`` when the turn itself raised.

``/v1/holds`` is the hold of a chat thread: the messages a person wrote while
a turn of that thread was still running. The hold lives here and not in the
page, so a reload or a closed tab never loses a message. ``POST`` adds one,
``GET`` lists them, ``DELETE`` takes one back before it goes, and
``POST /v1/holds/take`` empties the hold and returns its messages joined in
order, one per line, for the client to send as one turn. The hold lives in
memory, as the turns in flight do, so a restart of core clears it.

A message that waited longer than ``core.hold_max_age_s`` is dropped, and
every answer of the hold routes carries it in ``dropped``, so the person is
told that it was not sent. A held message goes with the next turn or it is
dropped; it never runs hours later on its own.

A message that arrives on a channel while a turn of that conversation runs is
folded in the same way: ``accept`` puts it in the fold of its conversation
instead of starting a second turn, and the end of the running turn sends
everything that waited as one turn, in order, one message per line. The fold
is per sender, so a group thread never puts the words of one person in the
mouth of another, and it carries the attachments of each folded message.

``/v1/drafts`` is the other half of a composer: the text a person wrote and
did not send. ``GET`` reads the draft of one thread and person, and ``POST``
keeps it, or clears it with an empty text. A draft is private to its person,
in a shared workspace too, and it is dropped when it was not touched for
``core.draft_max_age_s``. A draft waits for the person; a held message waits
for the turn.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.contracts import TurnEvent
from joshua_shared.fleet_auth import require_identity
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field

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

# How many messages one thread may hold, and how long one of them may be.
HOLD_MAX = 20
HOLD_MAX_CHARS = 8000
# How long a message may wait for the running turn, when the settings say
# nothing. ``core.hold_max_age_s`` is the setting.
HOLD_MAX_AGE_S = 900.0

# How long a draft is kept, and how long one may be. ``core.draft_max_age_s``
# is the setting for the age.
DRAFT_MAX_AGE_S = 604800.0
DRAFT_MAX_CHARS = 8000

# What a channel is told about a folded message that waited too long.
HOLD_DROPPED = (
    "A message that arrived while I was working waited too long, so I did not run it. "
    "Send it again if it still matters."
)


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
class HeldMessage:
    """One message that waits for the running turn of a thread to finish."""

    id: str
    text: str
    at: str
    # The monotonic clock when the hold took the message, for the age limit.
    held_at: float = 0.0

    def as_json(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text, "at": self.at}


def thread_key(channel: str, person: str) -> str:
    """The key of one thread and one person: the channel and who writes in it.

    It names both of that person's states in the thread, which are kept
    apart: the hold of what waits for the running turn, and the draft of what
    is not sent yet.
    """
    return f"{channel}\n{person}"


class HoldStore:
    """The messages a person wrote while a turn of the same thread ran.

    Every client of a thread sees the same list, so a reload, a second tab, or
    a tab that was closed finds the messages again. ``take`` empties the hold
    and gives back what was in it, in order: the caller sends it as one turn,
    and no two callers can send it twice.

    The hold is bounded in both directions: a thread holds ``max_held``
    messages, and a message that waited longer than ``max_age_s`` is dropped.
    ``dropped`` gives the dropped messages of a thread once, so a client that
    comes back tells the person which messages were not sent.
    """

    def __init__(
        self,
        max_held: int = HOLD_MAX,
        *,
        max_age_s: float = HOLD_MAX_AGE_S,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._max = max_held
        self._max_age = max_age_s
        self._clock = clock
        self._held: dict[str, list[HeldMessage]] = {}
        self._dropped: dict[str, list[HeldMessage]] = {}

    def _purge(self, key: str) -> None:
        """Move what waited past ``max_age_s`` out of the hold of a thread."""
        held = self._held.get(key)
        if not held or self._max_age <= 0:
            return
        cutoff = self._clock() - self._max_age
        old = [m for m in held if m.held_at < cutoff]
        if not old:
            return
        self._dropped.setdefault(key, []).extend(old)
        rest = [m for m in held if m.held_at >= cutoff]
        if rest:
            self._held[key] = rest
        else:
            self._held.pop(key, None)

    def list(self, key: str) -> list[HeldMessage]:
        self._purge(key)
        return list(self._held.get(key, []))

    def add(self, key: str, text: str) -> HeldMessage | None:
        """Hold one message. None when the thread already holds ``max_held``."""
        self._purge(key)
        held = self._held.setdefault(key, [])
        if len(held) >= self._max:
            return None
        message = HeldMessage(
            id=uuid4().hex[:12],
            text=text,
            at=datetime.now(UTC).isoformat(),
            held_at=self._clock(),
        )
        held.append(message)
        return message

    def remove(self, key: str, hold_id: str) -> bool:
        """Take one held message back. False when the hold has no such message."""
        self._purge(key)
        held = self._held.get(key, [])
        for index, message in enumerate(held):
            if message.id == hold_id:
                del held[index]
                if not held:
                    self._held.pop(key, None)
                return True
        return False

    def take(self, key: str) -> list[HeldMessage]:
        """Empty the hold of a thread and return what was in it, in order."""
        self._purge(key)
        return self._held.pop(key, [])

    def dropped(self, key: str) -> list[HeldMessage]:
        """The messages of a thread that waited too long, once."""
        self._purge(key)
        return self._dropped.pop(key, [])


def joined(held: list[HeldMessage]) -> str:
    """The held messages as the text of one turn: in order, one per line."""
    return "\n".join(message.text for message in held)


@dataclass
class Draft:
    """The text a person wrote in the composer of a thread and did not send."""

    text: str
    at: str
    # The monotonic clock of the last save, for the age limit.
    saved_at: float = 0.0

    def as_json(self) -> dict[str, str]:
        return {"text": self.text, "at": self.at}


class DraftStore:
    """The unsent text of each composer, by thread and by person.

    A draft waits for the person, where a held message waits for the turn, so
    the two are separate stores. A draft is keyed by the person who wrote it,
    so in a shared workspace one person's draft never reaches another
    person's composer. A draft that was not touched for ``max_age_s`` is
    dropped, so old drafts do not grow without limit. It lives in memory, as
    the hold does, so a restart of core clears it.
    """

    def __init__(
        self,
        *,
        max_age_s: float = DRAFT_MAX_AGE_S,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._max_age = max_age_s
        self._clock = clock
        self._drafts: dict[str, Draft] = {}

    def _purge(self) -> None:
        """Forget every draft that was not touched inside ``max_age_s``."""
        if self._max_age <= 0:
            return
        cutoff = self._clock() - self._max_age
        for key in [k for k, draft in self._drafts.items() if draft.saved_at < cutoff]:
            del self._drafts[key]

    def get(self, key: str) -> Draft | None:
        self._purge()
        return self._drafts.get(key)

    def save(self, key: str, text: str) -> Draft | None:
        """Keep the text of a composer, or forget the draft when it is empty."""
        self._purge()
        if not text:
            self._drafts.pop(key, None)
            return None
        draft = Draft(text=text, at=datetime.now(UTC).isoformat(), saved_at=self._clock())
        self._drafts[key] = draft
        return draft


@dataclass
class Resolution:
    """The routing result for one turn."""

    channel: Channel
    conversation: Conversation
    person: Person | None
    is_group: bool
    speaker: str | None
    # True for a viewer turn on the main thread of another channel: the turn
    # must not move the conversation's last channel.
    pull_only: bool = False


@dataclass
class FoldedMessage:
    """A channel message that waits for the running turn of its conversation."""

    resolution: Resolution
    event: TurnEvent
    # The monotonic clock when the fold took the message, for the age limit.
    held_at: float


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
        self._max_hold_age = float(settings.core.hold_max_age_s)
        self._holds = HoldStore(max_age_s=self._max_hold_age)
        self._drafts = DraftStore(max_age_s=float(settings.core.draft_max_age_s))
        # The messages of each conversation that wait for its running turn,
        # and how many turns of a conversation run now.
        self._folded: dict[str, list[FoldedMessage]] = {}
        self._active: dict[str, int] = {}
        self._background: set[asyncio.Task[Any]] = set()
        # The tasks of streamed turns. A task stays here until it ends, so no
        # one drops it while the client is gone.
        self._streamed: set[asyncio.Task[None]] = set()

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
        conversation = None
        pull_only = False
        if event.thread == "main" and handle.type == "cli" and person is not None and not is_group:
            # The viewer joins the person's main thread. Without one, the
            # terminal conversation stays the fallback.
            conversation = await self._repo.main_conversation(person.id)
            pull_only = conversation is not None
        if conversation is None:
            conversation = await self._repo.get_or_create_conversation(
                channel.id, conversation_person
            )

        speaker = None
        if is_group:
            speaker = person.display_name if person else handle.id

        return Resolution(
            channel=channel,
            conversation=conversation,
            person=person,
            is_group=is_group,
            speaker=speaker,
            pull_only=pull_only,
        )

    async def _resolve_workspace(
        self, event: TurnEvent, person: Person | None
    ) -> Resolution | None:
        """Resolve a turn on ``workspace:<id or slug>``.

        The workspace must exist, and the person of the turn must reach it: its
        owner, or a member of a shared one. Any other turn is refused: None,
        and the caller answers 403. A turn never creates a ``workspace:``
        channel. The ``workspaces`` routes create it with the workspace.

        A shared workspace is one thread that several people write in, so the
        turn runs as a group turn and carries the speaker: the message says who
        wrote it, and so does the transcript row.
        """
        if person is None or event.chat.kind == "group":
            return None
        ref = event.channel[len(WORKSPACE_PREFIX) :]
        workspace = await self._repo.find_workspace_for(person.id, ref)
        if workspace is None:
            logger.warning({"message": "workspace turn refused", "reason": "no such workspace"})
            return None
        channel = await self._repo.get_channel(workspace.channel_id)
        if channel is None:
            return None
        conversation = await self._repo.workspace_conversation(workspace)
        speaker = person.display_name if workspace.shared else None
        return Resolution(
            channel=channel,
            conversation=conversation,
            person=person,
            is_group=workspace.shared,
            speaker=speaker,
        )

    # --- run helpers -------------------------------------------------------

    def _engine_attachments(self, event: TurnEvent) -> list[EngineAttachment]:
        return [
            EngineAttachment(
                path=a.path, mime=a.mime, name=a.name or "", original_name=a.original_name
            )
            for a in event.attachments
        ]

    async def _run(
        self, resolution: Resolution, event: TurnEvent, on_delta: Any = None, **extra: Any
    ) -> Any:
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
            **({"pull_only": True} if resolution.pull_only else {}),
            **extra,
        )

    # --- the hold ----------------------------------------------------------

    def _hold_answer(self, key: str, **extra: Any) -> dict[str, Any]:
        """The hold of a thread as a client reads it: what waits, and what the
        age limit dropped since the last call."""
        held = [m.as_json() for m in self._holds.list(key)]
        dropped = [m.as_json() for m in self._holds.dropped(key)]
        return {"held": held, "dropped": dropped, **extra}

    def held(self, channel: str, person: str) -> dict[str, Any]:
        """The messages a thread holds, oldest first, and the dropped ones."""
        return self._hold_answer(thread_key(channel, person))

    def hold(self, channel: str, person: str, text: str) -> dict[str, Any] | None:
        """Hold one message of a thread, or None when the hold is full."""
        key = thread_key(channel, person)
        message = self._holds.add(key, text)
        if message is None:
            return None
        return self._hold_answer(key, message=message.as_json())

    def drop_held(self, channel: str, person: str, hold_id: str) -> dict[str, Any] | None:
        """Take one held message back before it goes, or None when it is gone."""
        key = thread_key(channel, person)
        if not self._holds.remove(key, hold_id):
            return None
        return self._hold_answer(key)

    def take_held(self, channel: str, person: str) -> dict[str, Any]:
        """Empty the hold of a thread: the messages and their text as one turn.

        A message that waited past the age limit is not in the text. It is in
        ``dropped``, so the person is told that it was not sent.
        """
        key = thread_key(channel, person)
        held = self._holds.take(key)
        dropped = [m.as_json() for m in self._holds.dropped(key)]
        return {"text": joined(held), "held": [m.as_json() for m in held], "dropped": dropped}

    # --- the draft of a composer -------------------------------------------

    def draft(self, channel: str, person: str) -> dict[str, Any]:
        """The unsent text of that person's composer in that thread. Empty
        when they have no draft there."""
        draft = self._drafts.get(thread_key(channel, person))
        return draft.as_json() if draft else {"text": "", "at": None}

    def save_draft(self, channel: str, person: str, text: str) -> dict[str, Any]:
        """Keep the unsent text of a composer. An empty text clears the
        draft, as a send does."""
        draft = self._drafts.save(thread_key(channel, person), text)
        return draft.as_json() if draft else {"text": "", "at": None}

    # --- the fold of a channel message -------------------------------------

    def _fold_key(self, resolution: Resolution, event: TurnEvent) -> str:
        """The fold of one sender in one conversation.

        A thread folds the messages of each sender on their own, so a turn
        never puts the words of one person in the mouth of another.
        """
        who = resolution.person.id if resolution.person else ""
        if not who and event.handle is not None:
            who = event.handle.id
        return f"{resolution.conversation.id}\n{who}"

    def _fold(self, resolution: Resolution, event: TurnEvent) -> bool:
        """Put one message in the fold of its conversation.

        False when the fold is full: the message then runs as a turn of its
        own, which core queues behind the running one.
        """
        key = self._fold_key(resolution, event)
        folded = self._folded.setdefault(key, [])
        if len(folded) >= HOLD_MAX:
            return False
        folded.append(FoldedMessage(resolution=resolution, event=event, held_at=monotonic()))
        logger.info(
            {
                "message": "message folded into the next turn",
                "channel": event.channel,
                "waiting": len(folded),
            }
        )
        return True

    @staticmethod
    def _folded_event(batch: list[FoldedMessage]) -> TurnEvent:
        """One turn from the folded messages of a sender: their texts in order,
        one per line, and the attachments of all of them. The idempotency key
        of a single message does not carry over."""
        texts = [m.event.text for m in batch if m.event.text]
        return batch[0].event.model_copy(
            update={
                "text": "\n".join(texts),
                "attachments": [a for m in batch for a in m.event.attachments],
                "message_id": None,
            }
        )

    def _flush_fold(self, conversation_id: str) -> None:
        """Send what arrived while a turn ran, as the next turn.

        A message that waited past ``core.hold_max_age_s`` is dropped instead,
        and the channel is told, so a message never runs hours late.
        """
        if self._active.get(conversation_id):
            return
        prefix = f"{conversation_id}\n"
        for key in [k for k in self._folded if k.startswith(prefix)]:
            batch = self._folded.pop(key)
            cutoff = monotonic() - self._max_hold_age if self._max_hold_age > 0 else None
            live = [m for m in batch if cutoff is None or m.held_at >= cutoff]
            if len(live) < len(batch):
                logger.info(
                    {
                        "message": "folded message dropped: it waited too long",
                        "channel": batch[0].event.channel,
                        "dropped": len(batch) - len(live),
                    }
                )
                self._tell(batch[0], HOLD_DROPPED)
            if live:
                self._begin(live[0].resolution, self._folded_event(live))

    def _tell(self, message: FoldedMessage, text: str) -> None:
        """Send one line to the channel of a folded message."""
        target = workspace_delivery_target(
            channel_type=message.resolution.channel.channel_type, target=message.event.channel
        )
        if target is None:
            return
        self._watch(asyncio.create_task(self._deliverer.deliver(target, text)))

    def _begin(self, resolution: Resolution, event: TurnEvent) -> None:
        """Run one turn in the background, counted against its conversation."""
        self._enter(resolution.conversation.id)
        self._watch(asyncio.create_task(self._run_and_deliver(resolution, event)))

    def _watch(self, task: asyncio.Task[Any]) -> None:
        """Keep a reference to a background task until it ends."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _enter(self, conversation_id: str) -> None:
        """Count one more turn of a conversation, so the next message folds."""
        self._active[conversation_id] = self._active.get(conversation_id, 0) + 1

    def _leave(self, conversation_id: str) -> None:
        """Count one turn of a conversation out."""
        left = self._active.get(conversation_id, 0) - 1
        if left > 0:
            self._active[conversation_id] = left
        else:
            self._active.pop(conversation_id, None)

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

        # A turn of this conversation runs: the message joins the next turn
        # instead of starting a second one.
        if self._active.get(resolution.conversation.id) and self._fold(resolution, event):
            return JSONResponse({"accepted": True, "folded": True}, status_code=202)

        if self._at_capacity():
            logger.warning({"message": "turn rejected: at capacity", "channel": event.channel})
            return JSONResponse({"accepted": False, "reason": "overloaded"}, status_code=429)

        turn_id = "t-" + resolution.conversation.id.replace("-", "")[:12]
        self._begin(resolution, event)
        return JSONResponse({"accepted": True, "turn_id": turn_id}, status_code=202)

    async def _run_and_deliver(self, resolution: Resolution, event: TurnEvent) -> None:
        try:
            await self._deliver_turn(resolution, event)
        finally:
            self._leave(resolution.conversation.id)
            self._flush_fold(resolution.conversation.id)

    async def _deliver_turn(self, resolution: Resolution, event: TurnEvent) -> None:
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

        turn_id = "t-" + uuid4().hex[:12]
        return StreamingResponse(
            self._sse(resolution, event, turn_id),
            media_type="text/event-stream",
        )

    async def _sse(self, resolution: Resolution, event: TurnEvent, turn_id: str) -> Any:
        """Start the turn in its own task and relay its events until it ends.

        This generator ends when the client leaves: Starlette cancels it, and
        the ``finally`` block marks the client gone. The task is not awaited
        there, and no cancellation reaches it, because it is a separate task
        that ``self._streamed`` holds. It stops writing to the queue and runs to
        the end.
        """
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        gone = False
        dropped = False
        # A message that arrives on a channel while this turn runs folds into
        # the turn that follows it.
        conversation_id = resolution.conversation.id
        self._enter(conversation_id)

        def put(name: str, data: dict[str, Any]) -> None:
            """Offer one event to the reader. It never raises into the turn."""
            nonlocal dropped
            if gone:
                return
            try:
                queue.put_nowait((name, data))
            except Exception as exc:  # noqa: BLE001 — an event is not worth a turn
                if not dropped:
                    dropped = True
                    logger.warning(
                        {
                            "message": "stream event dropped",
                            "turn_id": turn_id,
                            "event": name,
                            "error": str(exc),
                        }
                    )

        async def on_delta(chunk: str) -> None:
            put("delta", {"text": chunk})

        async def on_state(state: str) -> None:
            put("status", {"state": state})

        async def run() -> None:
            try:
                result = await self._run(
                    resolution, event, on_delta=on_delta, turn_id=turn_id, on_state=on_state
                )
                put(
                    "done",
                    {"turn_id": turn_id, "text": result.text or "", "tools": result.tools_used},
                )
            except asyncio.CancelledError:
                # Shutdown. The manager has already abandoned the client.
                logger.info(
                    {
                        "message": "stream turn cancelled",
                        "turn_id": turn_id,
                        "channel": event.channel,
                    }
                )
                put("error", {"message": "Joshua is restarting"})
                raise
            except Exception as exc:  # noqa: BLE001 — surface the failure as an SSE error
                logger.error(
                    {
                        "message": "stream turn failed",
                        "turn_id": turn_id,
                        "channel": event.channel,
                        "error": str(exc),
                    }
                )
                put("error", {"message": str(exc)})
            finally:
                self._leave(conversation_id)
                self._flush_fold(conversation_id)
                if gone:
                    logger.info(
                        {"message": "turn finished after the client left", "turn_id": turn_id}
                    )
                # The queue has no bound, so the sentinel always fits and this
                # never raises back into the turn.
                queue.put_nowait(None)

        task = asyncio.create_task(run(), name=f"stream-turn-{turn_id}")
        self._streamed.add(task)
        task.add_done_callback(self._streamed.discard)
        put("accepted", {"turn_id": turn_id})
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                name, data = item
                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
        finally:
            if not task.done():
                gone = True
                logger.info({"message": "client left a running turn", "turn_id": turn_id})

    async def drain(self) -> None:
        """Await every in-flight background turn. Called on shutdown.

        A turn that ends sends what its conversation folded as one more turn,
        so this waits until no task is left.
        """
        while tasks := [*self._background, *self._streamed]:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._events is not None:
            await self._events.drain()


# --- routes ----------------------------------------------------------------


def _service(request: Request) -> TurnService:
    return request.app.state.ctx.turns


class HoldBody(BaseModel):
    """The thread of a hold call, and the message a ``POST`` adds to it."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=200)
    person: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=HOLD_MAX_CHARS)


class DraftBody(BaseModel):
    """The composer of a draft call, and the text a ``POST`` keeps for it."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=200)
    person: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=DRAFT_MAX_CHARS)


def build_router() -> APIRouter:
    """The turns router. ``/v1/turns`` is channels-only; ``/v1/turns/stream``,
    the ``/v1/holds`` routes, and the ``/v1/drafts`` routes also accept
    ``laptop`` for a ``cli`` handle."""
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

    @router.get("/v1/holds")
    async def get_holds(  # type: ignore[no-untyped-def]
        request: Request,
        channel: str = Query(min_length=1, max_length=200),
        person: str = Query(min_length=1, max_length=64),
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        return JSONResponse(_service(request).held(channel, person))

    @router.post("/v1/holds")
    async def post_hold(  # type: ignore[no-untyped-def]
        body: HoldBody,
        request: Request,
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        text = body.text.strip()
        if not text:
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        answer = _service(request).hold(body.channel, body.person, text)
        if answer is None:
            return JSONResponse({"reason": "hold_full"}, status_code=429)
        return JSONResponse(answer)

    @router.post("/v1/holds/take")
    async def post_holds_take(  # type: ignore[no-untyped-def]
        body: HoldBody,
        request: Request,
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        return JSONResponse(_service(request).take_held(body.channel, body.person))

    @router.delete("/v1/holds/{hold_id}")
    async def delete_hold(  # type: ignore[no-untyped-def]
        hold_id: str,
        request: Request,
        channel: str = Query(min_length=1, max_length=200),
        person: str = Query(min_length=1, max_length=64),
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        answer = _service(request).drop_held(channel, person, hold_id)
        if answer is None:
            return JSONResponse({"reason": "unknown_hold"}, status_code=404)
        return JSONResponse(answer)

    @router.get("/v1/drafts")
    async def get_draft(  # type: ignore[no-untyped-def]
        request: Request,
        channel: str = Query(min_length=1, max_length=200),
        person: str = Query(min_length=1, max_length=64),
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        return JSONResponse(_service(request).draft(channel, person))

    @router.post("/v1/drafts")
    async def post_draft(  # type: ignore[no-untyped-def]
        body: DraftBody,
        request: Request,
        _identity: str = Depends(require_identity(["channels", "laptop"])),
    ):
        return JSONResponse(_service(request).save_draft(body.channel, body.person, body.text))

    return router
