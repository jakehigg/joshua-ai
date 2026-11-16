"""Web push: the VAPID keys of the instance, and the send of one notification.

A browser subscribes with the public VAPID key of the instance and gives back
an endpoint at its own push service (Apple, Google, Mozilla) and two keys.
This module signs a request to that endpoint (RFC 8292) and encrypts the
payload to those keys (RFC 8291), so the push service carries a notification it
cannot read. ``pywebpush`` does the encryption and ``py_vapid`` the signature;
the request itself goes out on ``httpx``, because core is async.

The sender runs in core, because the VAPID private key is a secret setting and
never leaves core (``store/settings.py``). The core container is therefore the
one that needs egress to the push services; see ``docs/notifications.md``.

**Off until the owner turns it on.** The four settings live in the settings
store, and no environment variable turns push on:

- ``push.enabled`` — ``true`` turns the routes and the sender on.
- ``push.vapid_public_key`` — what a browser subscribes with.
- ``push.vapid_private_key`` — a secret setting, encrypted at rest with
  ``JOSHUA_SETTINGS_KEY``, which no route ever answers.
- ``push.vapid_subject`` — ``mailto:you@example.com`` or ``https://joshua.example``,
  which a push service writes to about this instance.

With ``push.enabled`` false, one of the keys missing, or
``JOSHUA_SETTINGS_KEY`` unset, ``push_config`` answers no config and a reason:
every route turns that into a 503 (``push_api``) and the sender does nothing.
There is no connectivity check: a real send fails with the real error.

**The payload** holds ``title``, ``body``, ``url`` and ``tag``. The tag is the
workspace slug, so the browser collapses several notifications of one
workspace into the newest.

**A send that cannot be signed sends nothing.** The VAPID header takes the
private key and the subject, so a wrong one of either breaks every send and no
browser. ``_send`` builds the header of each endpoint before the first
request: when that raises, the report carries the message as its ``reason``,
nothing goes out, and no row is touched, so a test with a wrong subject costs
no subscription.

**What a refusal means.** A 404 or a 410 says the browser is gone, so the
subscription is deleted at once. A 429 is a rate limit, so the endpoint is held
until its ``Retry-After`` has passed and its failure count is untouched. Any
other refusal raises ``failures``, and a row that reaches ``FAILURE_LIMIT`` is
dropped (``store/push.py``). ``send_to_person`` never raises: a caller inside a
turn gets a report, never an exception.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from joshua_shared.log import get_logger
from py_vapid import Vapid02
from pywebpush import WebPusher

from joshua_core.store.push import PushSubscription
from joshua_core.store.repo import Repo
from joshua_core.store.settings import SettingsKeyError

logger = get_logger("push")

# The dotted prefix of every setting of this feature, and the four keys.
PUSH_PREFIX = "push"
ENABLED_KEY = "push.enabled"
PUBLIC_KEY = "push.vapid_public_key"
PRIVATE_KEY = "push.vapid_private_key"
SUBJECT_KEY = "push.vapid_subject"

# What a setting of ``push.enabled`` must say to turn push on.
TRUE_WORDS = ("true", "yes", "on", "1")

# How long a push service may hold a notification for a browser that is away,
# and how long one VAPID token is good for. Twelve hours is as long as
# RFC 8292 lets a token live.
TTL_S = 12 * 3600
TOKEN_TTL_S = 12 * 3600
# One push is a small request to a service that is either there or not.
TIMEOUT_S = 10.0

# A ``Retry-After`` that says nothing readable, and the longest one we honour.
DEFAULT_RETRY_AFTER_S = 60.0
MAX_RETRY_AFTER_S = 24 * 3600.0

OFF = "push notifications are off: the owner turns them on in Admin → Instance"
NO_KEYS = "push notifications have no VAPID keys yet: the owner sets them in Admin → Instance"
UNAVAILABLE = "push notifications are not available"

# The endpoints a push service told us to leave alone, and the moment each is
# free again. A 429 is a rate limit and not a dead browser, so the row stays as
# it is and the next send skips it until then. The map is this process's own:
# a restart simply tries again.
_BACKOFF: dict[str, datetime] = {}


@dataclass(frozen=True)
class PushConfig:
    """The VAPID identity of this instance, as the owner wrote it."""

    public_key: str
    private_key: str
    subject: str


@dataclass(frozen=True)
class Notification:
    """What a browser shows: a title, a line, where it goes, and its tag.

    ``url`` is a path of the app, such as ``/chat/w/shared/<slug>``, and never
    an absolute address: the service worker follows a path of its own origin
    and opens the app itself for anything else.

    ``tag`` is the workspace slug, so the browser replaces the notification of
    that workspace instead of stacking another one.
    """

    title: str
    body: str = ""
    url: str = ""
    tag: str = ""

    def payload(self) -> bytes:
        """The JSON the service worker reads, as the bytes that get encrypted."""
        return json.dumps(
            {"title": self.title, "body": self.body, "url": self.url, "tag": self.tag},
            ensure_ascii=False,
        ).encode("utf-8")


@dataclass(frozen=True)
class SendReport:
    """What one send did, for the log and for the answer of a route.

    ``reason`` says why nothing was sent at all: push is off, a key is
    missing, ``JOSHUA_SETTINGS_KEY`` is unset, or the VAPID header of the send
    would not build, which is a wrong subject or an unreadable private key.
    ``error`` names the last refusal of a push service, as a status or the
    name and the message of an exception. Neither one ever holds a key or a
    payload.
    """

    sent: int = 0
    failed: int = 0
    dropped: int = 0
    held: int = 0
    reason: str = ""
    error: str = ""


def is_true(value: str | None) -> bool:
    """Whether a stored setting says yes."""
    return (value or "").strip().lower() in TRUE_WORDS


async def push_config(repo: Repo) -> tuple[PushConfig | None, str]:
    """The push config of the instance, or ``(None, reason)``.

    The reason is a sentence for the person at the screen: push is off, the
    keys are not set, or ``JOSHUA_SETTINGS_KEY`` is missing, which is the
    error of the settings store itself. Nothing here reaches the network.
    """
    rows = {
        row.key: row.value
        for row in await repo.settings.get_many(PUSH_PREFIX, decrypt=False)
        if not row.is_secret
    }
    if not is_true(rows.get(ENABLED_KEY)):
        return None, OFF
    try:
        stored = await repo.settings.get(PRIVATE_KEY)
    except SettingsKeyError as exc:
        return None, str(exc)
    private = stored.value if stored is not None else ""
    public, subject = rows.get(PUBLIC_KEY, ""), rows.get(SUBJECT_KEY, "")
    if not (public and private and subject):
        return None, NO_KEYS
    return PushConfig(public_key=public, private_key=private, subject=subject), ""


def error_text(exc: Exception) -> str:
    """One exception as the line a page shows: its class and its message.

    ``py_vapid`` says "Invalid subject" and the settings store names
    ``JOSHUA_SETTINGS_KEY``, and a class name alone says neither. No key and
    no payload is ever in this text: it is the message of the library, which
    holds the shape of what was wrong and not the value.
    """
    return f"{type(exc).__name__}: {exc}"


def vapid_headers(config: PushConfig, endpoint: str, *, now: float | None = None) -> dict[str, str]:
    """The ``Authorization`` header of one request, signed with the private key.

    The audience is the scheme and host of the endpoint, as RFC 8292 asks, so a
    token for one push service is no use at another.
    """
    parts = urlsplit(endpoint)
    claims = {
        "sub": config.subject,
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((time.time() if now is None else now) + TOKEN_TTL_S),
    }
    return dict(Vapid02.from_string(config.private_key).sign(claims))


def encrypt(subscription: PushSubscription, payload: bytes) -> bytes:
    """The payload, encrypted to the keys of one browser (RFC 8291, aes128gcm)."""
    pusher = WebPusher(
        {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
    )
    return bytes(pusher.encode(payload)["body"])


def retry_after_seconds(value: str | None) -> float:
    """The seconds of a ``Retry-After`` header: a count, an HTTP date, or the
    default when it says neither. It is never longer than a day."""
    text = (value or "").strip()
    seconds = DEFAULT_RETRY_AFTER_S
    if text.isdigit():
        seconds = float(text)
    elif text:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def hold(endpoint: str, retry_after: str | None) -> datetime:
    """Leave one endpoint alone until its ``Retry-After`` has passed."""
    free_at = datetime.now(UTC) + timedelta(seconds=retry_after_seconds(retry_after))
    _BACKOFF[endpoint] = free_at
    return free_at


def held(endpoint: str) -> bool:
    """Whether a push service is still holding this endpoint off."""
    free_at = _BACKOFF.get(endpoint)
    if free_at is None:
        return False
    if free_at > datetime.now(UTC):
        return True
    del _BACKOFF[endpoint]
    return False


def clear_holds() -> None:
    """Forget every hold. The sender starts fresh after a restart anyway."""
    _BACKOFF.clear()


async def send_to_person(
    repo: Repo,
    person_id: str,
    notification: Notification,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> SendReport:
    """Send one notification to every browser of one person.

    It never raises: a caller inside a turn gets a report. With push off or a
    key missing the report carries the reason and nothing goes out.
    """
    try:
        return await _send(repo, person_id, notification, client_factory)
    except Exception as exc:  # a push must never break the turn that caused it
        logger.warning(
            {
                "message": "a push notification failed",
                "person": person_id,
                "error": type(exc).__name__,
            }
        )
        return SendReport(error=type(exc).__name__, reason=UNAVAILABLE)


async def _send(
    repo: Repo,
    person_id: str,
    notification: Notification,
    client_factory: Callable[[], httpx.AsyncClient] | None,
) -> SendReport:
    config, reason = await push_config(repo)
    if config is None:
        logger.info({"message": "push notification skipped", "person": person_id})
        return SendReport(reason=reason)
    subscriptions = await repo.push_subscriptions.list_for(person_id)
    if not subscriptions:
        return SendReport()
    try:
        # The signature is built for every endpoint before the first send: it
        # takes the private key and the subject, so what breaks it breaks the
        # whole send and is no fault of any browser. Nothing is sent and no
        # row is touched, so a test with a wrong subject costs no subscription.
        signed = {
            subscription.endpoint: vapid_headers(config, subscription.endpoint)
            for subscription in subscriptions
        }
    except Exception as exc:  # a wrong subject, or a key that does not read
        message = error_text(exc)
        logger.warning(
            {"message": "the VAPID header did not build", "person": person_id, "error": message}
        )
        return SendReport(reason=message)
    payload = notification.payload()
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=TIMEOUT_S))
    counts = {"sent": 0, "failed": 0, "dropped": 0, "held": 0}
    error = ""
    async with factory() as client:
        for subscription in subscriptions:
            if held(subscription.endpoint):
                counts["held"] += 1
                continue
            outcome, detail = await _deliver(
                client, repo, signed[subscription.endpoint], subscription, payload
            )
            counts[outcome] += 1
            if outcome != "sent" and detail:
                error = detail
    logger.info({"message": "push notification sent", "person": person_id, **counts})
    return SendReport(**counts, error=error)


async def _deliver(
    client: httpx.AsyncClient,
    repo: Repo,
    signature: dict[str, str],
    subscription: PushSubscription,
    payload: bytes,
) -> tuple[str, str]:
    """One notification to one browser: the outcome and what went wrong.

    ``signature`` is the VAPID header of this endpoint, built in ``_send``.

    The outcome is ``sent``, ``dropped`` (the row is gone), ``failed`` (the row
    kept one more failure) or ``held`` (a 429, so the row is untouched).
    """
    store = repo.push_subscriptions
    try:
        body = encrypt(subscription, payload)
        headers = {
            **signature,
            "content-encoding": "aes128gcm",
            "content-type": "application/octet-stream",
            "ttl": str(TTL_S),
            "urgency": "normal",
        }
    except Exception as exc:
        # The keys of this browser do not encrypt to, so the notification
        # cannot be built for it. It is a failure of this subscription alone,
        # and the next one still goes out.
        message = error_text(exc)
        logger.warning({"message": "a push could not be built", "error": message})
        gone = await store.mark_failure(subscription.endpoint)
        return ("dropped" if gone else "failed"), message
    try:
        response = await client.post(subscription.endpoint, content=body, headers=headers)
    except httpx.HTTPError as exc:
        gone = await store.mark_failure(subscription.endpoint)
        logger.warning({"message": "a push service is unreachable", "error": type(exc).__name__})
        return ("dropped" if gone else "failed"), type(exc).__name__
    status = response.status_code
    if 200 <= status < 300:
        await store.mark_ok(subscription.endpoint)
        return "sent", ""
    if status in (404, 410):
        # The browser is gone: the subscription goes with it, now.
        await store.delete(subscription.endpoint)
        logger.info({"message": "push subscription is gone", "status": status})
        return "dropped", str(status)
    if status == 429:
        free_at = hold(subscription.endpoint, response.headers.get("retry-after"))
        logger.info({"message": "a push service is rate limiting", "until": free_at.isoformat()})
        return "held", str(status)
    gone = await store.mark_failure(subscription.endpoint)
    logger.warning({"message": "a push service refused a notification", "status": status})
    return ("dropped" if gone else "failed"), str(status)
