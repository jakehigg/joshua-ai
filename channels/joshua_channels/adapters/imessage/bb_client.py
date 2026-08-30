"""BlueBubbles REST client.

BlueBubbles runs as a macOS app next to Messages.app and exposes an HTTP API
authenticated by a single shared password passed as a QUERY PARAMETER on every
call (``?password=…``). There is no bearer scheme, no signing, no per-caller
identity.

Send quirks this client exists to encapsulate:

  * The text-send body field is ``message``, NOT ``text``. Sending ``text`` is
    accepted with a 200 and delivers nothing.
  * ``method: "apple-script"`` is the only method available to us — the BB
    Private API is not enabled on this Mac. AppleScript drives Messages.app
    serially, so sends must be spaced out and are occasionally flaky.
  * ``tempGuid`` is MANDATORY for apple-script and is BB's idempotency key. A
    retry MUST reuse the same tempGuid, otherwise a send that actually landed
    is delivered twice.
  * A retry whose predecessor landed comes back as HTTP 400 with "already
    queued" in the body. That is SUCCESS, not an error.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from joshua_shared.log import get_logger

logger = get_logger("channels.imessage.bb_client")

SEND_METHOD = "apple-script"
# BB's "this tempGuid is already in flight" rejection. Case-insensitive
# substring match against the raw 400 body.
ALREADY_QUEUED = "already queued"


class BlueBubblesError(Exception):
    """A BB call that could not be completed (after retries, where they apply)."""


@dataclass(frozen=True)
class SendAttempt:
    """Outcome of one chunk send, for logging/tests."""

    temp_guid: str
    attempts: int
    already_queued: bool = False


def chunk_text(text: str, size: int) -> list[str]:
    """Split into at most ``size``-character pieces, in order.

    A hard split on purpose: iMessage has no chunk protocol, so any
    word-boundary cleverness only changes where the seam lands. Empty input
    yields no chunks (nothing to send).
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


class BlueBubblesClient:
    """Thin async wrapper over the BB REST API, sharing one httpx client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        password: str,
        *,
        timeout_s: float = 30.0,
        min_interval_s: float = 1.0,
        max_attempts: int = 3,
        backoff_s: Sequence[float] = (1.0, 3.0, 9.0),
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._password = password
        self._timeout = timeout_s
        self._min_interval_s = min_interval_s
        self._max_attempts = max(1, max_attempts)
        self._backoff = tuple(backoff_s) or (1.0,)
        # Global send throttle. AppleScript is serial on the Mac side, so two
        # concurrent sends interleave into the same Messages.app queue.
        self._send_lock = asyncio.Lock()
        self._last_send_at = 0.0
        # Held for a whole multi-chunk message so two sends can't interleave
        # their chunks.
        self._message_lock = asyncio.Lock()

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self._base and self._password)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"password": self._password, **extra}

    def _require_configured(self) -> None:
        if not self.configured:
            raise BlueBubblesError("BlueBubbles is not configured (password unset)")

    @staticmethod
    def _data(response: httpx.Response) -> Any:
        """BB wraps every JSON response as {status, message, data}."""
        try:
            body = response.json()
        except ValueError as exc:
            raise BlueBubblesError(f"non-JSON response from BlueBubbles: {exc}") from exc
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # ── read paths ───────────────────────────────────────────────────────

    async def ping(self) -> bool:
        """True when the BB server answers. Never raises — the caller is a
        health probe that must not flap the pod."""
        if not self.configured:
            return False
        try:
            resp = await self._client.get(
                self._url("/api/v1/ping"), params=self._params(), timeout=self._timeout
            )
            return resp.is_success
        except Exception as exc:  # noqa: BLE001 — health check, log and move on
            logger.warning({"message": "bluebubbles ping failed", "error": str(exc)})
            return False

    async def download_attachment(self, attachment_guid: str) -> bytes:
        """Raw attachment bytes. Raises BlueBubblesError on any failure."""
        self._require_configured()
        try:
            resp = await self._client.get(
                self._url(f"/api/v1/attachment/{attachment_guid}/download"),
                params=self._params(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise BlueBubblesError(f"attachment download failed: {exc}") from exc
        if not resp.is_success:
            raise BlueBubblesError(
                f"attachment download returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.content

    async def query_messages_since(
        self, after_ms: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent messages for the reconciler.

        ``after``/``before`` are millisecond epochs on BB's message query. The
        caller re-filters on ``dateCreated`` client-side, because the
        server-side semantics (created vs. delivered, inclusive vs. exclusive)
        are not contractual and a BB upgrade could change them under us.
        """
        self._require_configured()
        body = {
            "with": ["chats", "attachments", "handle"],
            "sort": "DESC",
            "limit": limit,
            "after": after_ms,
        }
        try:
            resp = await self._client.post(
                self._url("/api/v1/message/query"),
                params=self._params(),
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise BlueBubblesError(f"message query failed: {exc}") from exc
        if not resp.is_success:
            raise BlueBubblesError(f"message query returned {resp.status_code}: {resp.text[:200]}")
        data = self._data(resp)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    async def list_webhooks(self) -> list[dict[str, Any]]:
        self._require_configured()
        resp = await self._client.get(
            self._url("/api/v1/webhook"), params=self._params(), timeout=self._timeout
        )
        if not resp.is_success:
            raise BlueBubblesError(f"list webhooks returned {resp.status_code}: {resp.text[:200]}")
        data = self._data(resp)
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    async def register_webhook(
        self, url: str, events: Iterable[str] = ("new-message",)
    ) -> dict[str, Any]:
        """Point the BB server's webhook at this bridge.

        Registration is an operator action (the URL embeds the webhook path
        secret), not something startup does automatically — re-registering on
        every pod restart would pile up duplicate subscriptions on the Mac.
        """
        self._require_configured()
        resp = await self._client.post(
            self._url("/api/v1/webhook"),
            params=self._params(),
            json={"url": url, "events": list(events)},
            timeout=self._timeout,
        )
        if not resp.is_success:
            raise BlueBubblesError(
                f"register webhook returned {resp.status_code}: {resp.text[:200]}"
            )
        data = self._data(resp)
        return data if isinstance(data, dict) else {}

    # ── send path ────────────────────────────────────────────────────────

    async def _throttle(self) -> None:
        """Hold the global send lock long enough to space BB calls apart."""
        wait = self._min_interval_s - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send_at = time.monotonic()

    async def send_text(self, chat_guid: str, message: str) -> SendAttempt:
        """Send ONE chunk, with retries that reuse the same tempGuid.

        Returns on success; raises BlueBubblesError once attempts are exhausted.
        """
        self._require_configured()
        temp_guid = str(uuid.uuid4())
        body = {
            "chatGuid": chat_guid,
            "message": message,  # NOT "text" — BB silently drops "text"
            "method": SEND_METHOD,
            "tempGuid": temp_guid,  # idempotency key, stable across retries
        }
        return await self._send_with_retries(
            chat_guid,
            temp_guid,
            lambda: self._client.post(
                self._url("/api/v1/message/text"),
                params=self._params(),
                json=body,
                timeout=self._timeout,
            ),
        )

    async def send_attachment(self, chat_guid: str, path: Path) -> SendAttempt:
        """Send one file over the BB attachment API, retries reusing tempGuid.

        Outbound media over AppleScript is the least reliable BB path; the
        caller treats a failure as non-fatal (it sends a text note instead).
        """
        self._require_configured()
        temp_guid = str(uuid.uuid4())
        name = path.name

        def _post() -> Any:
            data = path.read_bytes()
            files = {"attachment": (name, data, "application/octet-stream")}
            form = {
                "chatGuid": chat_guid,
                "tempGuid": temp_guid,
                "name": name,
                "method": SEND_METHOD,
            }
            return self._client.post(
                self._url("/api/v1/message/attachment"),
                params=self._params(),
                data=form,
                files=files,
                timeout=self._timeout,
            )

        return await self._send_with_retries(chat_guid, temp_guid, _post)

    async def _send_with_retries(self, chat_guid: str, temp_guid: str, post: Any) -> SendAttempt:
        """Run ``post`` under the throttle, retry on failure, honor "already
        queued". ``post`` builds a fresh request each call so a retry re-reads
        any file body."""
        last_error = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._send_lock:
                    await self._throttle()
                    resp = await post()
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
            else:
                if resp.is_success:
                    return SendAttempt(temp_guid=temp_guid, attempts=attempt)
                raw = resp.text or ""
                if resp.status_code == 400 and ALREADY_QUEUED in raw.lower():
                    # The predecessor attempt actually landed. Retrying past
                    # this would double-send.
                    logger.info(
                        {
                            "message": "bluebubbles reports send already queued",
                            "temp_guid": temp_guid,
                            "chat_guid": chat_guid,
                            "attempt": attempt,
                        }
                    )
                    return SendAttempt(temp_guid=temp_guid, attempts=attempt, already_queued=True)
                last_error = f"HTTP {resp.status_code}: {raw[:200]}"

            logger.warning(
                {
                    "message": "bluebubbles send attempt failed",
                    "temp_guid": temp_guid,
                    "chat_guid": chat_guid,
                    "attempt": attempt,
                    "max_attempts": self._max_attempts,
                    "error": last_error,
                }
            )
            if attempt < self._max_attempts:
                delay = self._backoff[min(attempt - 1, len(self._backoff) - 1)]
                await asyncio.sleep(delay)

        raise BlueBubblesError(last_error)

    async def send_chunks(self, chat_guid: str, chunks: Sequence[str]) -> list[SendAttempt]:
        """Send chunks sequentially, in order, as one indivisible message.

        The message lock keeps a second concurrent send from interleaving its
        chunks into the middle of this one.
        """
        async with self._message_lock:
            results: list[SendAttempt] = []
            for chunk in chunks:
                results.append(await self.send_text(chat_guid, chunk))
            return results
