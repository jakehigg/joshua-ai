"""ConversationManager — the heart of the engine.

One warm session (SDK client + connected MCP servers) per active conversation,
serialized by a per-conversation lock, reaped when idle. Channels and the
scheduler funnel into ``run_turn``.

The prompt composer, the profile deriver, and the in-process builtin servers
(scheduling, registration) belong to other tickets and are injected, so this
module never imports them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from joshua_shared import wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.mcp import allowed_gateway_servers, gateway_servers
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.types import Attachment, OnDelta, TurnResult
from joshua_core.memory import prompt as memory_prompt
from joshua_core.store.models import Channel, Conversation
from joshua_core.store.repo import Repo

logger = get_logger("engine.manager")

# A per-conversation callback that returns extra prompt context for a turn, or
# None. Phase 4 re-adds memory and kb injection through this hook; it is empty now.
BuiltinFactory = Callable[[ToolDeps], Any]


@dataclass
class TurnCtx:
    """What a context provider sees for one turn."""

    channel: Channel
    conversation: Conversation
    text: str
    person_id: str | None
    turn_id: str
    profile: str = ""
    # The role this conversation carries: "member" or "guest". A taught skill
    # whose ``for`` names a role is matched against it. A guest is the safe
    # default, as it is everywhere else.
    role: str = "guest"
    # The message embedding, computed once and shared between providers (the
    # injection provider fills it; the taught-skill provider reuses it).
    embedding: list[float] | None = None


ContextProvider = Callable[[TurnCtx], Awaitable[str | None]]


@dataclass
class _Managed:
    conversation: Conversation
    session: Any  # AgentSession | StubAgentSession (duck-typed)
    lock: asyncio.Lock
    last_used: float
    deps: Any = None  # live ToolDeps for the SDK backend, else None
    profile: str = ""  # the derived profile name for this session
    # The profile's own idle deadline. None takes ``core.session_idle_seconds``.
    idle_ttl_s: int | None = None
    had_success: bool = False
    built_at: float = 0.0
    build_ms: int = 0
    # The memory-block source files and their newest mtime at build time. A newer
    # mtime (nightly rewrite of profile.md / shared/profile.md) triggers a rebuild.
    memory_paths: list[Path] = field(default_factory=list)
    memory_mtime: float = 0.0


JOURNAL_TOOL = "mcp__files__write_journal_entry"

WRITE_TOOLS = (
    "mcp__files__write_file",
    "mcp__files__rename_file",
    JOURNAL_TOOL,
)


def _journal_path(data: dict[str, Any], today: date | None) -> str | None:
    """Where ``write_journal_entry`` put its entry, from the call's arguments.

    The tool takes a ``slug``, never a path: the gateway places the file. So
    the path is rebuilt the way the gateway builds it, from ``date`` when the
    call carried one and from ``today`` otherwise. Both containers read the
    same ``timezone``, so both agree on the day.

    Returns None when the day cannot be known, which keeps a guess out of the
    log.
    """
    slug = data.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    raw = data.get("date")
    if isinstance(raw, str) and raw:
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            return None
    elif today is not None:
        day = today
    else:
        return None
    return f"wiki/journal/{day:%Y/%m/%d}/{slug}.md"


def written_paths(tool_calls: list[dict[str, Any]], *, today: date | None = None) -> list[str]:
    """The paths a turn actually wrote, in call order, without duplicates.

    A write the gateway refused is not here, so an operator reading the log
    does not look for a file that was never made.

    A call with no recorded outcome counts as failed. Silence is not success,
    and naming a file that does not exist is the fault this guards.

    A ``rename_file`` reports its destination, because that is where the content
    ends up. A ``write_journal_entry`` reports the path the gateway built for
    it; pass ``today`` in the instance timezone so an entry with no explicit
    date can be named.
    """
    paths: list[str] = []
    for call in tool_calls:
        name = call.get("name")
        if name not in WRITE_TOOLS or not call.get("ok", False):
            continue
        data = call.get("input") or {}
        if name == JOURNAL_TOOL:
            path = _journal_path(data, today)
        else:
            path = data.get("to") or data.get("path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


def failed_writes(tool_calls: list[dict[str, Any]]) -> int:
    """How many write calls the turn made that did not land.

    Only the count. A refused path is not logged, because a path can carry text
    a person sent.
    """
    return sum(
        1 for call in tool_calls if call.get("name") in WRITE_TOOLS and not call.get("ok", False)
    )


def _new_turn_id() -> str:
    return "t-" + uuid4().hex[:12]


def _chat_id(channel_id: str) -> str:
    """The platform chat id inside a namespaced channel id.

    ``telegram:-100200`` gives ``-100200``. ``groups_by_chat`` is keyed on the
    platform id, and the channel row keeps it namespaced.
    """
    _, _, rest = channel_id.partition(":")
    return rest or channel_id


class ConversationManager:
    def __init__(
        self,
        *,
        repo: Repo,
        settings: JoshuaConfig,
        composer: Any,
        derive_profile: Callable[[Any, Channel], Any],
        gateway_url: str = "",
        gateway_token: str = "",
        agent_backend: str = "sdk",
        data_dir: Path | str = "/data",
        builtin_factories: dict[str, BuiltinFactory] | None = None,
    ):
        self._repo = repo
        self._settings = settings
        # id -> journal preference; a missing id defaults to ``auto``.
        self._journal_modes = {p.id: p.journal for p in settings.people}
        self._composer = composer
        self._derive_profile = derive_profile
        self._gateway_url = gateway_url
        self._gateway_token = gateway_token
        self._agent_backend = agent_backend
        self._data_dir = Path(data_dir)
        # scheduling / registration builders, injected by their tickets.
        self._builtin_factories: dict[str, BuiltinFactory] = dict(builtin_factories or {})
        # Iterated before assembling each turn's prompt; empty until phase 4.
        self._context_providers: list[ContextProvider] = []
        self._pool: dict[str, _Managed] = {}
        self._pool_lock = asyncio.Lock()

    def register_builtin(self, name: str, factory: BuiltinFactory) -> None:
        """Wire an in-process MCP server builder (scheduling, registration)."""
        self._builtin_factories[name] = factory

    def add_context_provider(self, provider: ContextProvider) -> None:
        self._context_providers.append(provider)

    # --- session construction ---------------------------------------------

    def _session_cwd(self, conversation_id: str) -> Path:
        """The empty per-conversation scratch dir the SDK uses as its cwd. Nothing
        in it matters — the agent has no file tools."""
        cwd = self._data_dir / "inbox" / "sessions" / conversation_id
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    def _memory_block(self, person: Any, channel: Channel) -> Any:
        """The recency tier (profile page + shared profile) for a session."""
        mem = self._settings.memory
        return memory_prompt.build_memory_block(
            person,
            channel,
            shared_max_chars=mem.shared_max_chars,
            data_dir=self._data_dir,
        )

    async def _build_session(
        self, channel: Channel, conversation: Conversation, resume: str | None
    ) -> tuple[Any, Any, Any, list[Path], float]:
        """Build a session for a conversation. Returns
        ``(session, deps, profile, memory_paths, memory_mtime)`` — deps is the live
        ToolDeps for the SDK backend (so the manager rebinds its channel and speaker
        per turn), or None for the stub; profile is the derived profile, which
        carries the session's name and its ceilings; the
        memory fields let ``_get_or_create`` rebuild after a nightly profile
        rewrite."""
        person = (
            await self._repo.get_person(conversation.person_id) if conversation.person_id else None
        )
        profile = self._derive_profile(person, channel)
        memory = self._memory_block(person, channel)
        system_prompt = self._composer.compose(profile, person, channel, memory)
        mem_paths = memory_prompt.source_paths(person, channel, self._data_dir)
        mem_mtime = memory_prompt.newest_mtime(mem_paths)
        cwd = self._session_cwd(conversation.id)

        role = self._conversation_role(channel, conversation)
        gateway_names = allowed_gateway_servers(self._settings, conversation.person_id)
        server_names = [*gateway_names, *self._builtin_factories]

        if self._agent_backend == "stub":
            from joshua_core.engine.stub_agent import StubAgentSession

            session = StubAgentSession(
                system_prompt,
                cwd,
                conversation.id,
                server_names=server_names,
                resume=resume,
                data_dir=self._data_dir,
                wiki_git=wikigit.is_enabled(self._settings),
            )
            return session, None, profile, mem_paths, mem_mtime

        from joshua_core.engine.agent import AgentSession, build_options

        mcp = gateway_servers(
            gateway_names,
            self._gateway_url,
            self._gateway_token,
            conversation.person_id,
            conversation.id,
            role,
        )
        deps = ToolDeps(
            repo=self._repo,
            conversation=conversation,
            channel=channel,
            tz=self._settings.timezone,
            person_id=conversation.person_id,
        )
        for name, factory in self._builtin_factories.items():
            mcp[name] = factory(deps)
        options = build_options(
            system_prompt=system_prompt,
            cwd=cwd,
            mcp_servers=mcp,
            model=getattr(profile, "model", None) or self._settings.core.model,
            max_turns=getattr(profile, "max_turns", 0) or self._settings.core.max_turns,
            resume=resume,
        )
        return AgentSession(options), deps, profile, mem_paths, mem_mtime

    def _conversation_role(self, channel: Channel, conversation: Conversation) -> str:
        """The role this conversation carries, for the gateway file policy.

        A direct message carries the role of its person. A group carries the role
        derived from its ``members``: ``member`` only when every handle names a
        member. Anything else is a guest, which is the safe default for a chat
        core cannot vouch for.

        The role is a property of the conversation and not of the turn, because
        the SDK writes the gateway headers onto the command line of the CLI
        subprocess when the session starts. A per-turn role could not reach the
        gateway without rebuilding the session for every message.
        """
        if channel.session_mode != "shared" and conversation.person_id:
            person = self._settings.person(conversation.person_id)
            return person.role if person is not None else "guest"
        group = self._settings.groups_by_chat(channel.channel_type, _chat_id(channel.id))
        if group is None:
            return "guest"
        role, _ = self._settings.group_role(group)
        return role

    async def _get_or_create(self, channel: Channel, conversation: Conversation) -> _Managed:
        async with self._pool_lock:
            mc = self._pool.get(conversation.id)
            if mc is not None:
                await self._refresh_if_stale_locked(mc, channel)
                return mc
            session, deps, profile, mem_paths, mem_mtime = await self._build_session(
                channel, conversation, conversation.sdk_session_id
            )
            mc = _Managed(
                conversation=conversation,
                session=session,
                lock=asyncio.Lock(),
                last_used=monotonic(),
                deps=deps,
                profile=getattr(profile, "name", "") or "",
                idle_ttl_s=getattr(profile, "idle_ttl_s", None),
                built_at=monotonic(),
                memory_paths=mem_paths,
                memory_mtime=mem_mtime,
            )
            self._pool[conversation.id] = mc
            await self._enforce_pool_limit_locked()
            return mc

    async def _refresh_if_stale_locked(self, mc: _Managed, channel: Channel) -> None:
        """Rebuild a pooled session when its ``profile.md`` or ``shared/profile.md``
        changed on disk (the nightly rewrite). Called with ``_pool_lock`` held; a
        busy session keeps its prompt and rebuilds on a later turn. History carries
        over through the resumed sdk session."""
        if mc.lock.locked():
            return
        if memory_prompt.newest_mtime(mc.memory_paths) <= mc.memory_mtime:
            return
        old = mc.session
        session, deps, profile, mem_paths, mem_mtime = await self._build_session(
            channel, mc.conversation, mc.conversation.sdk_session_id
        )
        mc.session = session
        mc.deps = deps
        mc.profile = getattr(profile, "name", "") or ""
        mc.idle_ttl_s = getattr(profile, "idle_ttl_s", None)
        mc.memory_paths = mem_paths
        mc.memory_mtime = mem_mtime
        mc.built_at = monotonic()
        await old.close()
        logger.info(
            {"message": "session rebuilt (memory changed)", "conversation_id": mc.conversation.id}
        )

    async def _enforce_pool_limit_locked(self) -> None:
        """Called with ``_pool_lock`` held. Close least-recently-used idle sessions
        until the pool is back under ``core.pool_max``."""
        limit = self._settings.core.pool_max
        if len(self._pool) <= limit:
            return
        idle = sorted(
            (mc for mc in self._pool.values() if not mc.lock.locked()),
            key=lambda m: m.last_used,
        )
        n = len(self._pool)
        while n > limit and idle:
            victim = idle.pop(0)
            await victim.session.close()
            self._pool.pop(victim.conversation.id, None)
            n -= 1
            logger.info(
                {"message": "reaped session (pool cap)", "conversation_id": victim.conversation.id}
            )

    # --- turns -------------------------------------------------------------

    def _now_str(self) -> str:
        try:
            now = datetime.now(ZoneInfo(self._settings.timezone))
        except Exception:  # noqa: BLE001 — a bad tz name falls back to UTC
            now = datetime.now(ZoneInfo("UTC"))
        return now.strftime("%A, %B %d, %Y at %I:%M %p %Z")

    def _attach_note(self, attachments: list[Attachment] | None) -> str | None:
        if not attachments:
            return None
        lines = "\n".join(
            f"- {a.path}  ({a.mime}, original name: {a.original_name or a.name})"
            for a in attachments
        )
        return (
            "The user attached:\n"
            + lines
            + "\nUse the `files` tool `read_file(path)` to view each one before answering. "
            "Never describe a file you did not read. When you record this in a journal "
            "post, cite each file by its `attachments/…` path."
        )

    async def run_turn(
        self,
        channel: Channel,
        conversation: Conversation,
        text: str,
        *,
        on_delta: OnDelta | None = None,
        framing: str | None = None,
        direction: str = "in",
        speaker: str | None = None,
        person_id: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> TurnResult:
        turn_id = _new_turn_id()
        t_start = monotonic()
        mc = await self._get_or_create(channel, conversation)
        # The current turn's speaker. A per-person session carries a fixed
        # conversation.person_id; a shared turn passes the resolved sender.
        turn_person_id = person_id or conversation.person_id
        # A fresh local-time header each turn — the CLI injects the date, not the
        # clock time. Cheap and always current, unlike the cached system prompt.
        preamble = f"[Current local time: {self._now_str()}]"
        # A shared conversation labels the message with who sent it.
        body = f"{speaker}: {text}" if speaker else text
        attach_note = self._attach_note(attachments)

        ctx = TurnCtx(
            channel=channel,
            conversation=conversation,
            text=text,
            person_id=turn_person_id,
            turn_id=turn_id,
            profile=mc.profile,
            role=self._conversation_role(channel, conversation),
        )
        provider_notes: list[str] = []
        for provider in self._context_providers:
            note = await provider(ctx)
            if note:
                provider_notes.append(note)
        prompt = "\n\n".join(
            p for p in (preamble, framing, *provider_notes, attach_note, body) if p
        )

        # Time-to-first-output: stamp the first streamed delta even when the
        # channel does not consume deltas.
        first_output_at: list[float] = []

        async def _timed_delta(chunk: str) -> None:
            if not first_output_at:
                first_output_at.append(monotonic())
            if on_delta is not None:
                await on_delta(chunk)

        t_lock = monotonic()
        async with mc.lock:
            lock_wait_ms = round((monotonic() - t_lock) * 1000)
            mc.last_used = monotonic()
            # Bind the in-process tools to THIS turn's channel and speaker. Safe
            # under the per-conversation lock — turns are serialized.
            if mc.deps is not None:
                mc.deps.channel = channel
                mc.deps.person_id = turn_person_id
            # The stub backend journals to this turn's speaker; the SDK backend
            # ignores this call. The mode gates the canned ``[[journal]]`` write.
            set_turn = getattr(mc.session, "set_turn_context", None)
            if set_turn is not None:
                set_turn(turn_person_id, self._journal_modes.get(turn_person_id or "", "auto"))
            in_meta: dict[str, Any] = {"turn_id": turn_id}
            if speaker:
                in_meta["speaker"] = speaker
            await self._repo.add_transcript(conversation.id, direction, text, meta=in_meta)
            try:
                result = await self._run_with_resume_retry(
                    mc, channel, conversation, prompt, _timed_delta
                )
            except Exception as e:  # noqa: BLE001
                await self._repo.add_transcript(
                    conversation.id, "out", f"[error] {e}", status="failed"
                )
                logger.error(
                    {
                        "message": "turn failed",
                        "conversation_id": conversation.id,
                        "error": str(e),
                        "duration_ms": round((monotonic() - t_start) * 1000),
                    }
                )
                raise

            mc.had_success = True
            mc.last_used = monotonic()
            if result.session_id and result.session_id != mc.conversation.sdk_session_id:
                await self._repo.set_sdk_session(
                    conversation.id, result.session_id, last_channel_id=channel.id
                )
                mc.conversation = replace(mc.conversation, sdk_session_id=result.session_id)
            else:
                await self._repo.touch_conversation(conversation.id, last_channel_id=channel.id)

            metrics = {
                "turn_id": turn_id,
                "duration_ms": round((monotonic() - t_start) * 1000),
                "first_output_ms": (
                    round((first_output_at[0] - t_start) * 1000) if first_output_at else None
                ),
                "lock_wait_ms": lock_wait_ms,
                "call_count": len(result.tool_calls),
                "tools": result.tools_used,
                # Paths the agent wrote this turn. The nightly reflection reads
                # these to tell a skill file from a journal entry.
                "written": written_paths(
                    result.tool_calls,
                    today=datetime.now(ZoneInfo(self._settings.timezone)).date(),
                ),
                **(
                    {"write_failed": failed} if (failed := failed_writes(result.tool_calls)) else {}
                ),
            }
            await self._repo.add_transcript(
                conversation.id,
                "out",
                result.text or "",
                status="failed" if result.is_error else "ok",
                meta={"usage": result.usage, "cost_usd": result.cost_usd, **metrics},
            )
            logger.info(
                {
                    "message": "turn done",
                    "conversation_id": conversation.id,
                    "status": "failed" if result.is_error else "ok",
                    **metrics,
                }
            )
            return result

    async def _run_with_resume_retry(
        self,
        mc: _Managed,
        channel: Channel,
        conversation: Conversation,
        prompt: str,
        on_delta: OnDelta,
    ) -> TurnResult:
        try:
            return await mc.session.run(prompt, on_delta)
        except Exception as e:  # noqa: BLE001
            # A stale resume id (e.g. the session file is gone after a restart)
            # breaks the very first turn. Rebuild without resume and try once more.
            if mc.had_success or conversation.sdk_session_id is None:
                raise
            logger.warning(
                {
                    "message": "first turn failed; retrying with fresh session",
                    "conversation_id": conversation.id,
                    "error": str(e),
                }
            )
            await mc.session.close()
            (
                mc.session,
                mc.deps,
                mc.profile,
                mc.memory_paths,
                mc.memory_mtime,
            ) = await self._build_session(channel, conversation, resume=None)
            if mc.deps is not None:
                mc.deps.channel = channel
            return await mc.session.run(prompt, on_delta)

    # --- lifecycle ---------------------------------------------------------

    async def flush_sessions(self) -> int:
        """Drop every pooled session so the next turn reconnects fresh — the CLI
        fetches MCP tool lists once at connect, so this is how a changed upstream
        toolset reaches existing conversations. History survives via
        ``sdk_session_id``. Each session is closed behind its lock, so a mid-turn
        session finishes first."""
        async with self._pool_lock:
            entries = list(self._pool.values())
            self._pool.clear()
        for mc in entries:
            asyncio.create_task(self._close_when_idle(mc), name=f"flush-{mc.conversation.id}")
        if entries:
            logger.info({"message": "session pool flushed", "count": len(entries)})
        return len(entries)

    async def _close_when_idle(self, mc: _Managed) -> None:
        async with mc.lock:
            await mc.session.close()

    async def interrupt(self, conversation_id: str) -> None:
        mc = self._pool.get(conversation_id)
        if mc is not None:
            await mc.session.interrupt()

    async def _reap_idle(self) -> int:
        """Close every unlocked session that is idle past its own deadline.

        A session takes the deadline from its profile, and ``core.session_idle_seconds``
        when the profile names none. A voice session names a short one: a person
        who stops talking has left the room, and a warm session there holds a
        conversation open that nobody is in. A TTL of 0 or less turns the reaper
        off for that session — the pool cap is then the only guard.
        """
        default_ttl = self._settings.core.session_idle_seconds
        now = monotonic()
        async with self._pool_lock:
            stale = []
            for mc in self._pool.values():
                ttl = default_ttl if mc.idle_ttl_s is None else mc.idle_ttl_s
                if ttl <= 0 or mc.lock.locked():
                    continue
                if (now - mc.last_used) > ttl:
                    stale.append(mc)
            for mc in stale:
                await mc.session.close()
                self._pool.pop(mc.conversation.id, None)
                logger.info(
                    {"message": "reaped idle session", "conversation_id": mc.conversation.id}
                )
        return len(stale)

    async def reaper_loop(self, interval_s: int = 60) -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self._reap_idle()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error({"message": "reaper error", "error": str(e)})

    async def pool_snapshot(self) -> dict[str, Any]:
        """Read-only view of the warm pool for the monitor."""
        now = monotonic()
        async with self._pool_lock:
            items = list(self._pool.values())
        sessions = [
            {
                "conversation_id": mc.conversation.id,
                "channel_id": mc.conversation.channel_id,
                "person_id": mc.conversation.person_id,
                "connected": bool(getattr(mc.session, "connected", True)),
                "busy": mc.lock.locked(),
                "age_s": round(now - mc.built_at, 1) if mc.built_at else None,
                "idle_s": round(now - mc.last_used, 1),
                "build_ms": mc.build_ms or None,
            }
            for mc in items
        ]
        return {
            "count": len(sessions),
            "pool_max": self._settings.core.pool_max,
            "sessions": sessions,
        }

    async def admin_pool(self) -> dict[str, Any]:
        """Operator view of the warm pool for ``GET /admin/pool``."""
        now = monotonic()
        async with self._pool_lock:
            items = list(self._pool.values())
        sessions = [
            {
                "conversation_id": mc.conversation.id,
                "person_id": mc.conversation.person_id,
                "profile": mc.profile or None,
                "idle_s": round(now - mc.last_used, 1),
                "locked": mc.lock.locked(),
            }
            for mc in items
        ]
        return {"sessions": sessions, "max": self._settings.core.pool_max}

    async def shutdown(self) -> None:
        async with self._pool_lock:
            for mc in list(self._pool.values()):
                await mc.session.close()
            self._pool.clear()
        logger.info({"message": "manager shut down"})
