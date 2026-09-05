"""Core boot: load config, connect Postgres, apply schema, seed the roster.

The lifespan wires the ``AppContext`` (settings + repo; the engine, deliverer,
and scheduler join it in later tickets) and exposes ``/healthz`` and ``/readyz``.
Both probes are open and carry no secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from joshua_shared import config as config_module
from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger, install_healthcheck_filter

from joshua_core.admin import build_admin_router
from joshua_core.delivery import CORE_TOKEN_ENV, Deliverer, build_deliverer
from joshua_core.engine import injection, taught_skills
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.profiles import derive_profile
from joshua_core.engine.prompts import PromptComposer
from joshua_core.engine.tools import recall, registration, scheduling
from joshua_core.events import EventService, SkillRegistry, build_channels_resolver
from joshua_core.memory import embed
from joshua_core.memory.indexer import Indexer, IndexerLoop
from joshua_core.memory.nightly import NightlyReflector
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.sources.memos import build_memos_source, schedule_seconds
from joshua_core.memory.store import MemoryStore
from joshua_core.scheduler import Scheduler
from joshua_core.store.db import Database
from joshua_core.store.repo import Repo
from joshua_core.store.seed import seed_people
from joshua_core.turns import TurnService, build_router

logger = get_logger("core")

DATABASE_URL_ENV = "DATABASE_URL"
AGENT_BACKEND_ENV = "AGENT_BACKEND"
GATEWAY_URL_ENV = "GATEWAY_URL"
DATA_DIR_ENV = "JOSHUA_DATA_DIR"
_AGENT_BACKENDS = ("sdk", "stub")


@dataclass
class AppContext:
    """Live application state shared by the request handlers.

    The engine reads ``agent_backend`` to pick the session backend; ``turns`` runs
    and delivers inbound turns; ``scheduler`` fires due tasks in the background.
    """

    settings: JoshuaConfig
    repo: Repo
    agent_backend: str
    manager: ConversationManager
    deliverer: Deliverer
    turns: TurnService
    scheduler: Scheduler
    memory: MemoryStore
    indexer: Indexer
    reflector: NightlyReflector


def _database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(f"{DATABASE_URL_ENV} is not set")
    return url


def resolve_agent_backend(env: Mapping[str, str] | None = None) -> str:
    """Return the agent backend to run: ``sdk`` (default) or ``stub``.

    ``sdk`` runs real Claude through the Claude Agent SDK and needs
    ``CLAUDE_CODE_OAUTH_TOKEN`` and the bundled ``claude`` CLI. ``stub`` streams a
    canned reply with no SDK, token, or network. An unset value selects ``sdk``.
    Any other value is an error.
    """
    source = os.environ if env is None else env
    value = (source.get(AGENT_BACKEND_ENV) or "sdk").strip().lower()
    if value not in _AGENT_BACKENDS:
        raise RuntimeError(
            f"{AGENT_BACKEND_ENV} must be one of {', '.join(_AGENT_BACKENDS)}, got {value!r}"
        )
    return value


def _log_group_roles(settings) -> None:
    """Say what each group chat may do, and why.

    A group writes the wiki only when every handle in ``members`` names a
    member. One guest in a family chat therefore stops Joshua writing for the
    members too, and nothing on the screen says so. This line is where an
    operator finds out.
    """
    for group in settings.groups:
        role, blocked_by = settings.group_role(group)
        logger.info(
            {
                "message": "group role",
                "group": group.id,
                "channel": group.channel,
                "role": role,
                # The handle that held the group down to guest, or None when the
                # group is a member chat or lists nobody at all.
                "lowered_by": blocked_by,
            }
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_healthcheck_filter()
    settings = config_module.load()
    agent_backend = resolve_agent_backend()

    db = Database(_database_url(), max_size=settings.core.pool_max)
    await db.connect()
    await db.init_schema()
    repo = Repo(db)
    await seed_people(repo, settings)
    _bootstrap_layout(settings)

    manager = _build_manager(settings, repo, agent_backend)
    memory_store = MemoryStore(db.pool)
    indexer, indexer_loop = _build_memory(settings, memory_store, manager, repo)
    data_dir = os.environ.get(DATA_DIR_ENV, "/data")
    reflector = NightlyReflector(
        repo=repo, indexer=indexer, manager=manager, settings=settings, data_dir=data_dir
    )
    deliverer = build_deliverer()
    events = EventService(
        repo=repo,
        registry=SkillRegistry(settings.core.prompts_dir),
        resolver=build_channels_resolver(),
        manager=manager,
        deliverer=deliverer,
        prompts_dir=settings.core.prompts_dir,
    )
    turns = TurnService(
        repo=repo, settings=settings, manager=manager, deliverer=deliverer, events=events
    )
    scheduler = Scheduler(
        repo=repo,
        manager=manager,
        deliverer=deliverer,
        tz=settings.timezone,
        tick_seconds=settings.core.scheduler_tick_seconds,
        prompts_dir=settings.core.prompts_dir,
    )

    app.state.ctx = AppContext(
        settings=settings,
        repo=repo,
        agent_backend=agent_backend,
        manager=manager,
        deliverer=deliverer,
        turns=turns,
        scheduler=scheduler,
        memory=memory_store,
        indexer=indexer,
        reflector=reflector,
    )
    await scheduler.start()
    indexer_loop.start()
    await reflector.start()
    _log_group_roles(settings)
    logger.info({"message": "joshua-core up", "agent_backend": agent_backend})
    try:
        yield
    finally:
        logger.info({"message": "joshua-core shutting down"})
        await reflector.stop()
        await indexer_loop.stop()
        await scheduler.stop()
        await turns.drain()
        await manager.shutdown()
        await db.close()


def _build_manager(settings: JoshuaConfig, repo: Repo, agent_backend: str) -> ConversationManager:
    """Wire the conversation manager from config and the fleet environment.

    The gateway url and the core fleet token route the agent's MCP servers
    through the gateway. The ``registration`` and ``scheduling`` builtins register
    here.
    """
    person_prompts = {p.id: p.prompt for p in settings.people if p.prompt}
    person_journal = {p.id: p.journal for p in settings.people if p.journal != "auto"}
    composer = PromptComposer(
        settings.core.prompts_dir,
        person_prompts=person_prompts,
        person_journal=person_journal,
    )
    data_dir = os.environ.get(DATA_DIR_ENV, "/data")
    manager = ConversationManager(
        repo=repo,
        settings=settings,
        composer=composer,
        derive_profile=partial(derive_profile, voice=settings.channels.voice),
        gateway_url=os.environ.get(GATEWAY_URL_ENV, ""),
        gateway_token=os.environ.get(CORE_TOKEN_ENV, ""),
        agent_backend=agent_backend,
        data_dir=data_dir,
    )
    registration.register(manager, data_dir)
    scheduling.register(manager)
    return manager


def _build_memory(
    settings: JoshuaConfig, store: MemoryStore, manager: ConversationManager, repo: Repo
) -> tuple[Indexer, IndexerLoop]:
    """Wire the in-core RAG: the ``files`` source (always on), any optional
    source adapter from ``memory.sources``, the indexer, its interval loop, the
    ``recall`` builtin tool, the per-turn injection provider, and the taught-skill
    provider. A source with a bad config, or a name with no adapter yet, is logged
    and skipped rather than failing boot."""
    data_dir = os.environ.get(DATA_DIR_ENV, "/data")
    memory = settings.memory

    sources: dict[str, Any] = {"files": FilesSource(data_dir)}
    intervals: dict[str, float] = {"files": float(memory.index_interval_s)}
    for name, options in memory.sources.items():
        if name == "files":
            continue
        if name == "memos":
            try:
                sources[name] = build_memos_source(options)
                intervals[name] = schedule_seconds(options.get("schedule"))
            except (ValueError, KeyError) as exc:
                logger.error(
                    {"message": "memos source config invalid; skipping", "error": str(exc)}
                )
            continue
        logger.warning({"message": "memory source adapter not available yet", "source": name})
    indexer = Indexer(
        store, sources, embed_model=memory.embed_model, chunk_chars=memory.chunk_chars
    )
    recall.register(manager, store, settings)
    injection.register(manager, store, settings, repo)
    taught_skills.register(manager, store, settings, repo)
    return indexer, IndexerLoop(indexer, intervals)


def _bootstrap_layout(settings: JoshuaConfig) -> None:
    """Create the data volume tree, then bring an older layout onto it.

    `migrate_to_one_wiki` runs before any template page is written, so a
    profile it finds at its target is a real one, never a template this
    boot wrote first — a template written before the move ran would
    otherwise shadow the real profile forever. Idempotent: a second boot
    finds the wiki and its pages already settled and leaves them.
    `migrate_to_one_wiki` logs its own counts, so this function does not log
    them again. The shipped documentation under `wiki/joshua-docs/` is
    rewritten each boot, because the repo owns it.
    """
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    layout.migrate_to_one_wiki()
    layout.bootstrap_shared_profile(settings.name)
    pages = layout.bootstrap_docs()
    if pages:
        logger.info({"message": "repo docs published", "pages": pages})
    for person in settings.people:
        layout.bootstrap_person(person.id, person.name)
    _sync_wiki_git(settings)


def _sync_wiki_git(settings: JoshuaConfig) -> None:
    """Keep `wiki/` a git repository Joshua commits. See `wiki.git`.

    The first run on an existing wiki commits every page; a later start
    commits whatever changed outside Joshua (a person's editor, a sync
    tool). A no-op when `wiki.git` is off.
    """
    if not wikigit.is_enabled(settings):
        return
    wiki_root = layout.wiki_root()
    wikigit.ensure_repo(wiki_root)
    wikigit.commit(wiki_root, None, "start: sync the wiki")


async def healthz() -> dict[str, bool]:
    return {"ok": True}


async def readyz(request: Request) -> JSONResponse:
    """Readiness: the DB answers a trivial query and the data layout is good.

    The status code carries the answer: 200 when ready, 503 when not. A probe
    reads the code and nothing else, so a body that says ``ok: false`` under a
    200 is a probe that can never fail.

    ``checks.embed`` reports the embedding model, and does not hold ``ok`` down.
    A lost model costs the memory, not the turn, so core stays in service and
    answers. The check makes the loss visible to a probe, because the symptom
    is otherwise a healthy container that remembers nothing.

    No secrets, no inventory.
    """
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    db_ok = False
    if ctx is not None:
        try:
            async with ctx.repo._pool.connection() as conn:  # noqa: SLF001 — readiness probe
                await conn.execute("SELECT 1")
            db_ok = True
        except Exception:  # noqa: BLE001 — any failure means not ready
            db_ok = False
    layout_ok = not layout.validate_layout()
    embed_ok = embed.is_available()
    ok = db_ok and layout_ok
    return JSONResponse(
        {"ok": ok, "checks": {"db": db_ok, "layout": layout_ok, "embed": embed_ok}},
        status_code=200 if ok else 503,
    )


def build_app() -> FastAPI:
    app = FastAPI(title="joshua-core", lifespan=lifespan)
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.include_router(build_router())
    app.include_router(build_admin_router())
    return app


app = build_app()
