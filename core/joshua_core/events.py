"""Events and event-skills: ``kind == "event"`` turns.

An external system (Home Assistant, a cron job, another service) reaches Joshua
through the channels webhook adapter. Channels lands any files and posts a
``TurnEvent`` with ``kind == "event"`` to ``POST /v1/turns``. Core routes it here.

Two shapes arrive:

- A **named event** carries an ``event_type``. Core looks it up in the skill
  registry — Markdown files under ``<prompts_dir>/skills/<event_type>.md`` — and,
  when a file exists, runs it as one turn on the skill's destination channel, then
  delivers the reply there. An unknown ``event_type`` is ignored.
- A **channel-addressed event** carries a ``channel`` and ``text``. With
  ``options.verbatim`` core records one ``inject`` transcript row and returns — no
  model call, no delivery, because channels already delivered the text. Without
  ``verbatim`` core runs the text as one turn framed as an injection, then delivers
  the reply.

The registry replaces the old hard-coded Python skill list: a self-hoster adds a
skill by dropping a Markdown file, with no code change. The kernel ships zero
skills; ``docs/events.md`` shows how to add one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi.responses import JSONResponse
from joshua_shared.contracts import TurnEvent
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger

from joshua_core.delivery import CHANNELS_URL_ENV, CORE_TOKEN_ENV, Deliverer, is_ignore
from joshua_core.engine.types import Attachment as EngineAttachment
from joshua_core.store.models import Channel
from joshua_core.store.repo import Repo

logger = get_logger("events")

# The channels endpoint that maps a destination name (e.g. ``everyone``) to a
# concrete channel id and chat kind.
RESOLVE_PATH = "/v1/channels/resolve"

# A skill file name must be a plain slug — no path separators, so a caller cannot
# reach outside the skills directory.
_EVENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Default framing for a named-event skill turn. A skill may override it in its
# frontmatter ``framing`` key.
SKILL_FRAMING = (
    "This is an automated skill firing now — not a live message from a person. "
    "Follow the instruction below and use the event data with it. Produce only "
    "the message that should reach the destination on this channel. If nothing "
    "needs to be said, reply with exactly `[IGNORE]`."
)

# Fallback framing for a channel-addressed event when the packaged ``event.md``
# prompt file is missing. Kept in sync with ``prompts/builtin/event.md``.
INJECT_FALLBACK = (
    "A webhook event fired and is described below. An event is not a person and "
    "not a live message — no one is waiting on a reply. If it needs a message to "
    "someone, phrase it the way it should reach them on this channel. If it "
    "needs quiet work, do it and stay silent. If nothing needs to be said, reply "
    "with exactly `[IGNORE]`."
)


@dataclass(frozen=True)
class Skill:
    """One event skill loaded from a Markdown file.

    ``prompt`` is the file body. ``destination`` is the channel the reply goes to
    (a name channels resolves, e.g. ``everyone``). ``framing`` overrides
    ``SKILL_FRAMING`` when the frontmatter sets it.
    """

    event_type: str
    destination: str
    prompt: str
    framing: str | None = None


@dataclass(frozen=True)
class ResolvedChannel:
    """What channels returns for a destination name."""

    channel_id: str
    chat_kind: str  # "dm" | "group"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a Markdown file into a ``(frontmatter, body)`` pair.

    The frontmatter is an optional block between two lines of exactly ``---`` at
    the top of the file. Only simple ``key: value`` pairs are read; a value keeps
    its text with surrounding quotes removed. A file with no frontmatter returns an
    empty map and the whole text as the body.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body = "\n".join(lines[i + 1 :]).strip()
            return meta, body
        key, sep, value = lines[i].partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("\"'")
    # Unterminated frontmatter — treat the whole file as body.
    return {}, text.strip()


def append_event_payload(prompt: str, payload: dict[str, Any] | None) -> str:
    """Append an event payload to a prompt as a fenced JSON block.

    Returns the prompt unchanged when there is no payload.
    """
    if not payload:
        return prompt
    block = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    return f"{prompt}\n\n```json\n{block}\n```"


class SkillRegistry:
    """Load event skills from Markdown files under ``<prompts_dir>/skills``.

    There is no Python list to edit: ``get(event_type)`` reads
    ``skills/<event_type>.md`` on demand. A missing file, an invalid event type,
    or a file with no ``destination`` returns None.
    """

    def __init__(self, prompts_dir: Path | str | None = None) -> None:
        base = Path(prompts_dir) if prompts_dir else Path(__file__).parent / "prompts"
        self._dir = base / "skills"

    def get(self, event_type: str) -> Skill | None:
        if not _EVENT_TYPE.match(event_type):
            return None
        path = self._dir / f"{event_type}.md"
        try:
            text = path.read_text()
        except OSError:
            return None
        meta, body = _parse_frontmatter(text)
        destination = meta.get("destination", "").strip()
        if not destination:
            logger.warning(
                {"message": "skill file has no destination; ignoring", "event_type": event_type}
            )
            return None
        return Skill(
            event_type=event_type,
            destination=destination,
            prompt=body,
            framing=meta.get("framing") or None,
        )


Resolver = Callable[[str], Awaitable[ResolvedChannel | None]]


class ChannelsResolver:
    """Ask channels to resolve a destination name to a channel id and chat kind."""

    def __init__(self, client: FleetClient) -> None:
        self._client = client

    async def resolve(self, ref: str) -> ResolvedChannel | None:
        try:
            response = await self._client.get_json(RESOLVE_PATH, params={"ref": ref})
        except httpx.ConnectError:
            logger.warning({"message": "channel resolve failed: connection error", "ref": ref})
            return None
        if response.status_code != 200:
            logger.warning(
                {"message": "channel resolve failed", "ref": ref, "status": response.status_code}
            )
            return None
        data = response.json()
        channel_id = data.get("channel_id")
        if not channel_id:
            return None
        return ResolvedChannel(channel_id=channel_id, chat_kind=data.get("chat_kind") or "dm")


def build_channels_resolver(
    env: Mapping[str, str] | None = None, *, timeout: float = 15.0
) -> ChannelsResolver:
    """Build a ``ChannelsResolver`` from the environment.

    Reads ``CHANNELS_URL`` (the channels base URL) and ``JOSHUA_TOKEN_CORE`` (the
    bearer core sends). Raises ``RuntimeError`` when ``CHANNELS_URL`` is not set.
    """
    source = os.environ if env is None else env
    base_url = source.get(CHANNELS_URL_ENV)
    if not base_url:
        raise RuntimeError(f"{CHANNELS_URL_ENV} is not set")
    client = FleetClient(base_url, source.get(CORE_TOKEN_ENV, ""), timeout=timeout)
    return ChannelsResolver(client)


class EventService:
    """Route ``kind == "event"`` turns to skills, injections, or verbatim delivery."""

    def __init__(
        self,
        *,
        repo: Repo,
        registry: SkillRegistry,
        resolver: Resolver | ChannelsResolver,
        manager: Any,
        deliverer: Deliverer,
        prompts_dir: Path | str | None = None,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._resolver = resolver
        self._manager = manager
        self._deliverer = deliverer
        base = Path(prompts_dir) if prompts_dir else Path(__file__).parent / "prompts"
        self._event_file = base / "builtin" / "event.md"
        self._background: set[asyncio.Task[None]] = set()

    # --- entry point -------------------------------------------------------

    async def handle(self, event: TurnEvent) -> JSONResponse:
        """Route one event. Named events look up a skill; the rest are
        channel-addressed."""
        if event.event_type:
            return await self._named(event)
        return await self._channel_addressed(event)

    async def drain(self) -> None:
        """Await every in-flight background event turn. Called on shutdown."""
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # --- named events ------------------------------------------------------

    async def _named(self, event: TurnEvent) -> JSONResponse:
        skill = self._registry.get(event.event_type or "")
        if skill is None:
            logger.info({"message": "event ignored: unknown type", "event_type": event.event_type})
            return JSONResponse({"ignored": True}, status_code=200)

        resolved = await self._resolve(skill.destination)
        if resolved is None:
            logger.warning(
                {
                    "message": "event skill destination unresolved",
                    "event_type": event.event_type,
                    "destination": skill.destination,
                }
            )
            return JSONResponse(
                {"ignored": True, "reason": "unresolved_destination"}, status_code=200
            )

        channel = await self._ensure_channel(resolved)
        person_id = None if channel.session_mode == "shared" else channel.default_person_id
        conversation = await self._repo.get_or_create_conversation(channel.id, person_id)
        prompt = append_event_payload(skill.prompt, event.payload)
        framing = skill.framing or SKILL_FRAMING

        self._spawn(
            self._run_and_deliver(
                channel=channel,
                conversation=conversation,
                text=prompt,
                framing=framing,
                target=skill.destination,
                event=event,
            )
        )
        return JSONResponse({"accepted": True, "event_type": event.event_type}, status_code=202)

    # --- channel-addressed events -----------------------------------------

    async def _channel_addressed(self, event: TurnEvent) -> JSONResponse:
        channel = await self._repo.resolve_channel(event.channel)
        if channel is None:
            logger.warning(
                {"message": "channel-addressed event: unknown channel", "channel": event.channel}
            )
            return JSONResponse({"ignored": True, "reason": "unknown_channel"}, status_code=200)

        person_id = None if channel.session_mode == "shared" else channel.default_person_id
        conversation = await self._repo.get_or_create_conversation(channel.id, person_id)

        if event.options and event.options.verbatim:
            await self._repo.add_transcript(
                conversation.id, "inject", event.text, meta={"event": True}
            )
            return JSONResponse({"accepted": True, "verbatim": True}, status_code=200)

        self._spawn(
            self._run_and_deliver(
                channel=channel,
                conversation=conversation,
                text=event.text,
                framing=self._inject_framing(),
                target=event.channel,
                event=event,
            )
        )
        return JSONResponse({"accepted": True}, status_code=202)

    # --- shared run path ---------------------------------------------------

    async def _run_and_deliver(
        self,
        *,
        channel: Channel,
        conversation: Any,
        text: str,
        framing: str,
        target: str,
        event: TurnEvent,
    ) -> None:
        try:
            result = await self._manager.run_turn(
                channel,
                conversation,
                text,
                framing=framing,
                direction="inject",
                attachments=self._engine_attachments(event),
            )
        except Exception as exc:  # noqa: BLE001 — a failed event turn must not crash the loop
            logger.error({"message": "event turn failed", "target": target, "error": str(exc)})
            return
        reply = result.text or ""
        if reply and not is_ignore(reply):
            await self._deliverer.deliver(target, reply)

    # --- helpers -----------------------------------------------------------

    async def _resolve(self, ref: str) -> ResolvedChannel | None:
        if isinstance(self._resolver, ChannelsResolver):
            return await self._resolver.resolve(ref)
        return await self._resolver(ref)

    async def _ensure_channel(self, resolved: ResolvedChannel) -> Channel:
        """Return the local channel row for a resolved destination, creating it on
        first use so the turn has a conversation to run in."""
        existing = await self._repo.get_channel(resolved.channel_id)
        if existing is not None:
            return existing
        channel_type = resolved.channel_id.split(":", 1)[0]
        session_mode = "shared" if resolved.chat_kind == "group" else "per_person"
        await self._repo.upsert_channel(
            resolved.channel_id, channel_type, session_mode=session_mode
        )
        channel = await self._repo.get_channel(resolved.channel_id)
        assert channel is not None
        return channel

    def _engine_attachments(self, event: TurnEvent) -> list[EngineAttachment]:
        return [
            EngineAttachment(path=a.path, mime=a.mime, name=a.name or "") for a in event.attachments
        ]

    def _inject_framing(self) -> str:
        try:
            return self._event_file.read_text().strip()
        except OSError:
            return INJECT_FALLBACK

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._background.add(task)
        task.add_done_callback(self._background.discard)
