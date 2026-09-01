"""Channels boot: load config, register adapters, serve the core-facing API.

The lifespan builds the ``ChannelsContext`` (settings, adapter registry, core
client) on startup and starts every adapter. With no channel section in config it
logs a warning and still serves ``/healthz``, ``/readyz``, and ``/v1/deliver``
(which answers 404 for every target). Both probes are open and carry no secrets.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from joshua_shared import config as config_module
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger, install_healthcheck_filter

from joshua_channels import deliver, webhooks
from joshua_channels.adapters.cli import CliAdapter
from joshua_channels.adapters.imessage import IMessageAdapter, build_webhook_router
from joshua_channels.adapters.telegram import TelegramAdapter
from joshua_channels.admin import build_admin_router
from joshua_channels.attachments import AttachmentPipeline, retention_loop
from joshua_channels.cli_routes import build_cli_router
from joshua_channels.core_client import CoreClient, build_core_client
from joshua_channels.deliver import DEFAULT_DATA_DIR, ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry

logger = get_logger("channels")

DATA_DIR_ENV = "JOSHUA_DATA_DIR"


def telegram_enabled(settings: JoshuaConfig) -> bool:
    """True when the telegram section is present and its bot token is set."""
    telegram = settings.channels.telegram
    return telegram is not None and bool(telegram.bot_token.strip())


def imessage_enabled(settings: JoshuaConfig) -> bool:
    """True when the imessage section is present and its credentials are set."""
    imessage = settings.channels.imessage
    if imessage is None:
        return False
    return bool(imessage.bluebubbles_password.strip() and imessage.webhook_path_secret.strip())


def configured_channel_types(settings: JoshuaConfig) -> list[str]:
    """Return the platform sections that have credentials (``telegram``/``imessage``).

    A section with an empty credential is off. This keeps the documented rule
    that an empty ``TELEGRAM_BOT_TOKEN`` runs the stack without Telegram.
    """
    types: list[str] = []
    if telegram_enabled(settings):
        types.append("telegram")
    if imessage_enabled(settings):
        types.append("imessage")
    return types


def build_context() -> tuple[ChannelsContext, CoreClient | None]:
    """Build the runtime context from config and the environment.

    Concrete adapters register in their own tickets; this skeleton wires the
    registry, the core client, and the destination resolver. A config with no
    channel section logs a warning.
    """
    settings = config_module.load()
    registry = AdapterRegistry()
    if not configured_channel_types(settings):
        logger.warning(
            {"message": "no platform channel configured; the terminal channel still works"}
        )
    core_client = build_core_client()
    data_dir = os.environ.get(DATA_DIR_ENV, DEFAULT_DATA_DIR)
    guard = Guard(settings, settings.channels.limits, config_provider=config_module.load)
    pipeline = AttachmentPipeline(
        data_dir=data_dir,
        retention_days=settings.channels.limits.attachment_retention_days,
        keep_originals=settings.channels.limits.keep_originals,
        timezone=settings.timezone,
    )
    register_adapters(registry, settings, guard, core_client, data_dir, pipeline)
    context = ChannelsContext(
        settings=settings,
        registry=registry,
        data_dir=data_dir,
        guard=guard,
        core_client=core_client,
        pipeline=pipeline,
    )
    return context, core_client


def register_adapters(
    registry: AdapterRegistry,
    settings: JoshuaConfig,
    guard: Guard,
    core_client: CoreClient | None,
    data_dir: str,
    pipeline: AttachmentPipeline,
) -> None:
    """Register one adapter per configured channel section.

    Every adapter shares the one attachment pipeline: it downloads files into an
    inbox directory and hands them to the pipeline for normalization and storage.
    """
    # The terminal channel is always on. It needs no credential, so a person on
    # the host can always reach Joshua.
    registry.register(CliAdapter(data_dir=data_dir))
    if settings.channels.telegram is not None and not telegram_enabled(settings):
        logger.warning({"message": "telegram configured with an empty bot token; channel is off"})
    if telegram_enabled(settings):
        registry.register(
            TelegramAdapter(
                bot_token=settings.channels.telegram.bot_token,
                guard=guard,
                core_client=core_client,
                settings_provider=config_module.load,
                pipeline=pipeline,
                data_dir=data_dir,
            )
        )
    if settings.channels.imessage is not None and not imessage_enabled(settings):
        logger.warning({"message": "imessage configured with an empty credential; channel is off"})
    if imessage_enabled(settings):
        imessage = settings.channels.imessage
        assert imessage is not None
        registry.register(
            IMessageAdapter(
                bluebubbles_url=imessage.bluebubbles_url,
                bluebubbles_password=imessage.bluebubbles_password,
                webhook_path_secret=imessage.webhook_path_secret,
                guard=guard,
                core_client=core_client,
                settings_provider=config_module.load,
                pipeline=pipeline,
                data_dir=data_dir,
                max_attachment_bytes=settings.channels.limits.max_attachment_bytes,
                coalesce_window_s=imessage.coalesce_window_s,
                stale_max_age_s=imessage.stale_max_age_s,
                reconcile_interval_s=imessage.reconcile_interval_s,
                reconcile_lookback_s=imessage.reconcile_lookback_s,
                send_chunk_chars=imessage.send_chunk_chars,
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_healthcheck_filter()
    context = getattr(app.state, "ctx", None)
    core_client: CoreClient | None = getattr(app.state, "core_client", None)
    started = False
    retention_task: asyncio.Task | None = None
    if context is None:
        context, core_client = build_context()
        app.state.ctx = context
        app.state.core_client = core_client
        if context.pipeline is not None:
            removed = context.pipeline.purge_stale_inbox()
            if removed:
                logger.info({"message": "stale inbox purged", "removed": removed})
            retention_task = asyncio.create_task(
                retention_loop(context.pipeline), name="attachment-retention"
            )
        await context.registry.start_all()
        started = True
        logger.info({"message": "joshua-channels up", "channels": context.registry.types()})
    try:
        yield
    finally:
        if started:
            logger.info({"message": "joshua-channels shutting down"})
            if retention_task is not None:
                retention_task.cancel()
                try:
                    await retention_task
                except asyncio.CancelledError:
                    pass
            await context.registry.stop_all()
            if core_client is not None:
                await core_client.aclose()


async def healthz() -> dict[str, bool]:
    return {"ok": True}


async def readyz(request: Request) -> JSONResponse:
    """Readiness: one entry per registered adapter, keyed by ``channel_type``.

    Each entry is ``{"ok": bool}`` plus a short ``reason`` when the adapter raised
    on its last poll. ``ok`` is False when any registered adapter is failing. A
    channel that is off has no adapter, so it is not a fault. No entry holds a
    secret.

    The status code carries the answer: 200 when ready, 503 when not. A probe
    reads the code and nothing else, so a body that says ``ok: false`` under a
    200 is a probe that can never fail.
    """
    checks: dict[str, object] = {}
    ok = True
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is not None:
        for channel_type in ctx.registry.types():
            adapter = ctx.registry.get(channel_type)
            if adapter is None:
                continue
            health = await adapter.health()
            checks[channel_type] = health.as_dict()
            if not health.ok:
                ok = False
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)


def build_app(context: ChannelsContext | None = None) -> FastAPI:
    """Build the channels app. Pass ``context`` to skip the config-backed lifespan."""
    app = FastAPI(title="joshua-channels", lifespan=lifespan)
    app.add_api_route("/healthz", healthz, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.include_router(deliver.build_router())
    app.include_router(webhooks.build_events_router())
    app.include_router(build_webhook_router())
    app.include_router(build_cli_router())
    app.include_router(build_admin_router())
    if context is not None:
        app.state.ctx = context
    return app


app = build_app()
