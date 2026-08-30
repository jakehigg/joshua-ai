"""The iMessage adapter: a BlueBubbles webhook in, BlueBubbles sends out.

Inbound is a webhook with a pull backstop (the reconciler), because BlueBubbles
fires each webhook once and never retries. The webhook route validates the path
secret, normalizes the payload, and enqueues it; all I/O happens on the single
forwarder worker, which applies the inbound ``Guard``, runs the attachment
pipeline, and submits one ``TurnEvent`` to core. Outbound is core → this adapter
→ BlueBubbles over the AppleScript send method.

BlueBubbles quirks this adapter exists to absorb: webhooks are unauthenticated
and never retried (the path secret is the only credential, a wrong one gets
404); the send field is ``message`` not ``text``; ``tempGuid`` is the mandatory
idempotency key and a "already queued" 400 means success; our own sends echo
back with ``isFromMe`` and a null group handle; the Mac sleeps, so BlueBubbles
reachability is reported on ``/readyz`` but never enforced.
"""

from __future__ import annotations

import secrets as secrets_mod
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_channels.adapters.imessage.bb_client import (
    BlueBubblesClient,
    BlueBubblesError,
    chunk_text,
)
from joshua_channels.adapters.imessage.dedupe import GuidDedupe
from joshua_channels.adapters.imessage.forwarder import AttachmentPipeline, Forwarder
from joshua_channels.adapters.imessage.normalize import unwrap_webhook
from joshua_channels.adapters.imessage.reconcile import Reconciler
from joshua_channels.core_client import CoreClient
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterHealth

logger = get_logger("channels.imessage")

CHANNEL_TYPE = "imessage"

# Only this BlueBubbles event type is processed; every other type is ignored.
WEBHOOK_EVENT = "new-message"

# The iMessage DM chat guid is ``iMessage;-;<handle>``; a group guid is looked
# up from config.
_SERVICE = "iMessage"
_DM_PREFIX = "dm:"
_GROUP_PREFIX = "group:"


class PingCache:
    """``GET /api/v1/ping`` result, cached for a TTL.

    k8s probes ``/readyz`` every few seconds; hitting the Mac that often is both
    wasteful and a good way to make a flaky BB server look like a flaky pod.
    """

    def __init__(self, bb: BlueBubblesClient, ttl_s: float = 30.0) -> None:
        self._bb = bb
        self._ttl_s = ttl_s
        self._value = False
        self._checked_at = 0.0

    async def reachable(self) -> bool:
        now = time.monotonic()
        if self._checked_at and (now - self._checked_at) < self._ttl_s:
            return self._value
        self._value = await self._bb.ping()
        self._checked_at = now
        return self._value


class IMessageAdapter:
    """One BlueBubbles bridge: inbound webhook + reconciler, outbound send."""

    channel_type = CHANNEL_TYPE

    def __init__(
        self,
        *,
        bluebubbles_url: str,
        bluebubbles_password: str,
        webhook_path_secret: str,
        guard: Guard,
        core_client: CoreClient | None,
        settings_provider: Callable[[], JoshuaConfig],
        pipeline: AttachmentPipeline | None = None,
        data_dir: str = "/data",
        max_attachment_bytes: int = 26214400,
        coalesce_window_s: float = 2.0,
        stale_max_age_s: float = 900.0,
        reconcile_interval_s: int = 60,
        reconcile_lookback_s: int = 300,
        send_chunk_chars: int = 4000,
        ping_ttl_s: float = 30.0,
        bb: BlueBubblesClient | None = None,
    ) -> None:
        self._webhook_path_secret = webhook_path_secret
        self._settings_provider = settings_provider
        self._send_chunk_chars = send_chunk_chars

        if bb is not None:
            self._http: httpx.AsyncClient | None = None
            self._bb = bb
        else:
            self._http = httpx.AsyncClient(follow_redirects=False)
            self._bb = BlueBubblesClient(self._http, bluebubbles_url, bluebubbles_password)

        self._forwarder = Forwarder(
            self._bb,
            guard=guard,
            core_client=core_client,
            settings_provider=settings_provider,
            pipeline=pipeline,
            data_dir=data_dir,
            max_attachment_bytes=max_attachment_bytes,
            dedupe=GuidDedupe(),
            coalesce_window_s=coalesce_window_s,
            stale_max_age_s=stale_max_age_s,
        )
        self._reconciler = Reconciler(
            self._bb,
            self._forwarder,
            interval_s=reconcile_interval_s,
            lookback_s=reconcile_lookback_s,
        )
        self._ping = PingCache(self._bb, ping_ttl_s)

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the forwarder worker and the reconciler poller."""
        self._forwarder.start()
        self._reconciler.start()
        logger.info(
            {
                "message": "imessage adapter started",
                "webhook_enabled": bool(self._webhook_path_secret),
            }
        )

    async def stop(self) -> None:
        """Stop the reconciler, drain the worker, and close the HTTP client."""
        await self._reconciler.stop()
        await self._forwarder.stop()
        if self._http is not None:
            await self._http.aclose()
        logger.info({"message": "imessage adapter stopped"})

    # --- inbound webhook ---------------------------------------------------

    def verify_secret(self, secret: str) -> bool:
        """Constant-time compare against the configured path secret.

        An unset secret rejects every request, so the route stays 404 when
        inbound is not configured.
        """
        configured = self._webhook_path_secret
        if not configured:
            return False
        return secrets_mod.compare_digest(secret, configured)

    def handle_webhook(self, body: Any) -> dict[str, Any]:
        """Filter the event type and enqueue. Never blocks — BB never retries."""
        event_type, data = unwrap_webhook(body)
        if event_type != WEBHOOK_EVENT:
            logger.debug({"message": "webhook event ignored", "type": event_type})
            return {"ignored": event_type}
        status = self._forwarder.submit(data, source="webhook")
        return {"ok": True, "status": status}

    async def reachable(self) -> bool:
        """BlueBubbles reachability, cached — reported on ``/readyz``."""
        return await self._ping.reachable()

    async def health(self) -> AdapterHealth:
        """Report the adapter state for ``/readyz``.

        ``ok`` stays True: the Mac sleeps, so BlueBubbles reachability is
        reported in ``bluebubbles``, never enforced.
        """
        return AdapterHealth(ok=True, detail={"bluebubbles": await self.reachable()})

    # --- outbound ----------------------------------------------------------

    async def send(self, chat_id: str, text: str, attachments: list[Path]) -> int:
        """Send a reply as chunked text plus one BlueBubbles send per attachment.

        Returns the number of BlueBubbles messages sent. Outbound media over
        AppleScript is unreliable; a failed attachment does not fail the reply —
        the adapter logs it and sends a short text note instead.
        """
        chunks = chunk_text(text, self._send_chunk_chars)
        parts = 0
        if chunks:
            await self._bb.send_chunks(chat_id, chunks)
            parts += len(chunks)
        for path in attachments:
            parts += await self._send_one_attachment(chat_id, path)
        return parts

    async def _send_one_attachment(self, chat_id: str, path: Path) -> int:
        try:
            await self._bb.send_attachment(chat_id, path)
            return 1
        except (BlueBubblesError, OSError) as exc:
            logger.warning(
                {
                    "message": "attachment send failed; sending a note",
                    "chat_id": chat_id,
                    "error": str(exc),
                }
            )
            try:
                await self._bb.send_chunks(chat_id, [f"(could not send attachment: {path.name})"])
                return 1
            except BlueBubblesError:
                logger.warning({"message": "attachment note send failed", "chat_id": chat_id})
                return 0

    # --- resolve -----------------------------------------------------------

    def resolve_ref(self, ref: str) -> str | None:
        """Map ``dm:<person_id>`` to an ``iMessage;-;<handle>`` guid, or
        ``group:<group_id>`` to the configured chat guid. None when unknown."""
        cfg = self._settings_provider()
        if ref.startswith(_DM_PREFIX):
            person = cfg.person(ref[len(_DM_PREFIX) :])
            if person is None:
                return None
            handle = person.handles.get(CHANNEL_TYPE)
            return f"{_SERVICE};-;{handle}" if handle else None
        if ref.startswith(_GROUP_PREFIX):
            group_id = ref[len(_GROUP_PREFIX) :]
            for group in cfg.groups:
                if group.channel == CHANNEL_TYPE and group.id == group_id:
                    return group.chat_id
            return None
        return None


def build_webhook_router() -> APIRouter:
    """The BlueBubbles inbound route: ``POST /webhook/imessage/{secret}``.

    The route reads the live adapter from ``request.app.state.ctx`` at call
    time, because adapters are built in the app lifespan. A wrong secret or an
    unconfigured iMessage channel gets 404, so a prober cannot confirm the route
    exists. A valid call always answers 200 fast — BlueBubbles never retries.
    """
    router = APIRouter()

    @router.post("/webhook/imessage/{secret}")
    async def imessage_webhook(secret: str, request: Request):  # type: ignore[no-untyped-def]
        ctx = getattr(request.app.state, "ctx", None)
        adapter = ctx.registry.get(CHANNEL_TYPE) if ctx is not None else None
        if adapter is None or not adapter.verify_secret(secret):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed body, nothing to retry
            logger.warning({"message": "webhook body was not json"})
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=200)
        return JSONResponse(adapter.handle_webhook(body), status_code=200)

    return router
