"""Tests for web push: the signature, the encryption, and what a refusal does.

The push service is a ``httpx.MockTransport``, so nothing here reaches the
network. The subscriptions and the settings are fakes of the two stores
(``store/push.py`` and ``store/settings.py``); the real SQL of each one is
covered by ``test_push_integration.py``.

The tests cover the VAPID signature of a request (RFC 8292), the encryption of
the payload to the keys of a browser (RFC 8291), the header that will not build
and costs no subscription, the 410 that deletes a subscription, the 429 that is
honoured and does not count as a failure, the threshold that drops a
subscription, and the four routes, which answer 503 while push is off or a key
is missing and 400 when the send itself could not be built.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from joshua_core import push
from joshua_core.push import (
    ENABLED_KEY,
    OFF,
    PRIVATE_KEY,
    PUBLIC_KEY,
    SUBJECT_KEY,
    Notification,
    encrypt,
    push_config,
    send_to_person,
    vapid_headers,
)
from joshua_core.push_api import NO_SUBSCRIPTION, build_push_router
from joshua_core.store.push import FAILURE_LIMIT, PushSubscription
from joshua_core.store.settings import SETTINGS_KEY_ENV, Setting, encrypt_value
from py_vapid import Vapid02
from starlette.testclient import TestClient

VIEWER = "viewer-token"
CHANNELS = "channels-token"
MIRA = "mira"
SUBJECT = "mailto:owner@example.test"
ENDPOINT = "https://push.test/send/one"
SECOND_ENDPOINT = "https://push.test/send/two"

KEY_PATH = "/v1/push/key"
SUBSCRIBE_PATH = "/v1/push/subscribe"
TEST_PATH = "/v1/push/test"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_bytes(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Browser:
    """One subscribed browser: the keys it gave, and the private half it kept."""

    p256dh: str
    auth: str
    private: ec.EllipticCurvePrivateKey
    secret: bytes

    def read(self, body: bytes) -> dict[str, Any]:
        """The payload this browser reads out of an encrypted body."""
        plain = http_ece.decrypt(
            body, private_key=self.private, auth_secret=self.secret, version="aes128gcm"
        )
        return json.loads(plain)


def a_browser() -> Browser:
    private = ec.generate_private_key(ec.SECP256R1())
    secret = os.urandom(16)
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return Browser(_b64url(public), _b64url(secret), private, secret)


def a_vapid_pair() -> tuple[str, str]:
    """One VAPID pair, as the owner would paste it: base64url, public first."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


class FakeSettingsStore:
    """``repo.settings`` in a dict, with the store's own encryption."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, bool]] = {}

    def write(self, key: str, value: str, *, is_secret: bool = False) -> None:
        self.rows[key] = (encrypt_value(key, value) if is_secret else value, is_secret)

    async def get(self, key: str, *, decrypt: bool = True) -> Setting | None:
        from joshua_core.store.settings import decrypt_value

        row = self.rows.get(key)
        if row is None:
            return None
        stored, is_secret = row
        value = stored
        if is_secret:
            value = decrypt_value(key, stored) if decrypt and stored else ""
        return Setting(key=key, value=value, is_secret=is_secret, is_set=bool(stored))

    async def get_many(self, prefix: str, *, decrypt: bool = True) -> list[Setting]:
        keys = sorted(k for k in self.rows if k == prefix or k.startswith(f"{prefix}."))
        rows = [await self.get(key, decrypt=decrypt) for key in keys]
        return [row for row in rows if row is not None]


class FakePushStore:
    """``repo.push_subscriptions`` in a dict, keyed by endpoint."""

    def __init__(self) -> None:
        self.rows: dict[str, PushSubscription] = {}

    def add(self, person_id: str, endpoint: str, browser: Browser) -> PushSubscription:
        row = PushSubscription(
            id=endpoint,
            person_id=person_id,
            endpoint=endpoint,
            p256dh=browser.p256dh,
            auth=browser.auth,
            created_at=datetime.now(UTC),
        )
        self.rows[endpoint] = row
        return row

    async def upsert(
        self,
        person_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        *,
        user_agent: str = "",
    ) -> PushSubscription:
        row = PushSubscription(
            id=endpoint,
            person_id=person_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
        )
        self.rows[endpoint] = row
        return row

    async def list_for(self, person_id: str) -> list[PushSubscription]:
        return [row for row in self.rows.values() if row.person_id == person_id]

    async def delete(self, endpoint: str, *, person_id: str | None = None) -> bool:
        row = self.rows.get(endpoint)
        if row is None or (person_id is not None and row.person_id != person_id):
            return False
        del self.rows[endpoint]
        return True

    async def mark_ok(self, endpoint: str) -> None:
        row = self.rows.get(endpoint)
        if row is not None:
            self.rows[endpoint] = replace(row, last_ok_at=datetime.now(UTC), failures=0)

    async def mark_failure(self, endpoint: str) -> bool:
        row = self.rows.get(endpoint)
        if row is None:
            return True
        failures = row.failures + 1
        if failures >= FAILURE_LIMIT:
            del self.rows[endpoint]
            return True
        self.rows[endpoint] = replace(row, failures=failures)
        return False


class PushService:
    """A fake push service: it records each request and answers as told."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 201
        self.headers: dict[str, str] = {}

    def answer(self, status: int, **headers: str) -> None:
        self.status, self.headers = status, headers

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers)

    def factory(self):  # type: ignore[no-untyped-def]
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def settings_key(monkeypatch) -> None:
    monkeypatch.setenv(SETTINGS_KEY_ENV, base64.b64encode(os.urandom(32)).decode("ascii"))


@pytest.fixture(autouse=True)
def no_holds() -> None:
    """No test inherits a rate limit from another one."""
    push.clear_holds()


@pytest.fixture
def repo(settings_key) -> SimpleNamespace:
    """A repo of the two fakes, with push on and a fresh VAPID pair."""
    settings = FakeSettingsStore()
    public, private = a_vapid_pair()
    settings.write(ENABLED_KEY, "true")
    settings.write(PUBLIC_KEY, public)
    settings.write(PRIVATE_KEY, private, is_secret=True)
    settings.write(SUBJECT_KEY, SUBJECT)
    fake = SimpleNamespace(settings=settings, push_subscriptions=FakePushStore())
    fake.vapid_public = public  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def service() -> PushService:
    return PushService()


@pytest.fixture
def client(monkeypatch, repo) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        repo=repo,
        settings=SimpleNamespace(
            person=lambda person_id: (
                SimpleNamespace(id=MIRA, role="member") if person_id == MIRA else None
            )
        ),
    )
    app.include_router(build_push_router())
    return TestClient(app)


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER}"}


def _note() -> Notification:
    return Notification(title="Joshua", body="one line", url="/chat", tag="a-workspace")


# --- the signature ----------------------------------------------------------


async def test_the_request_is_signed_for_that_push_service(repo) -> None:
    config, reason = await push_config(repo)
    assert (config, reason) != (None, OFF) and config is not None
    headers = vapid_headers(config, ENDPOINT)
    scheme, _, rest = headers["Authorization"].partition(" ")
    assert scheme == "vapid"
    parts = dict(piece.split("=", 1) for piece in rest.split(","))
    assert parts["k"] == repo.vapid_public
    header, claims, signature = parts["t"].split(".")
    body = json.loads(_b64url_bytes(claims))
    # The audience is the scheme and host of the endpoint, and nothing else.
    assert body["aud"] == "https://push.test"
    assert body["sub"] == SUBJECT
    assert time.time() < body["exp"] <= time.time() + push.TOKEN_TTL_S
    # The signature reads back under the public key the browser subscribed with.
    verifier = Vapid02.from_raw_public(repo.vapid_public.encode())
    assert verifier.verify_token(f"{header}.{claims}".encode(), signature)


# --- the encryption ---------------------------------------------------------


def test_the_payload_is_encrypted_to_the_keys_of_the_browser() -> None:
    browser = a_browser()
    subscription = PushSubscription(
        id="1", person_id=MIRA, endpoint=ENDPOINT, p256dh=browser.p256dh, auth=browser.auth
    )
    note = _note()
    body = encrypt(subscription, note.payload())
    assert note.title.encode() not in body
    assert browser.read(body) == {
        "title": "Joshua",
        "body": "one line",
        "url": "/chat",
        "tag": "a-workspace",
    }


async def test_a_send_posts_the_encrypted_payload_and_notes_the_success(repo, service) -> None:
    browser = a_browser()
    repo.push_subscriptions.add(MIRA, ENDPOINT, browser)
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.failed, report.dropped, report.held) == (1, 0, 0, 0)
    request = service.requests[-1]
    assert str(request.url) == ENDPOINT
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == str(push.TTL_S)
    assert request.headers["authorization"].startswith("vapid ")
    assert browser.read(request.content)["tag"] == "a-workspace"
    row = repo.push_subscriptions.rows[ENDPOINT]
    assert row.last_ok_at is not None and row.failures == 0


# --- what a refusal does ----------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
async def test_a_dead_endpoint_is_deleted_on_the_next_send(repo, service, status) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    service.answer(status)
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.dropped) == (0, 1)
    assert repo.push_subscriptions.rows == {}


async def test_a_rate_limit_is_honoured_and_is_not_a_failure(repo, service) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    service.answer(429, **{"retry-after": "120"})
    first = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (first.held, first.failed, first.dropped) == (1, 0, 0)
    assert repo.push_subscriptions.rows[ENDPOINT].failures == 0
    # The next send leaves that endpoint alone: the service hears nothing.
    before = len(service.requests)
    second = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert second.held == 1
    assert len(service.requests) == before
    assert ENDPOINT in repo.push_subscriptions.rows
    # Once the Retry-After has passed, the endpoint is tried again.
    push._BACKOFF[ENDPOINT] = datetime.now(UTC) - timedelta(seconds=1)
    service.answer(201)
    third = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert third.sent == 1


@pytest.mark.parametrize(
    ("header", "low", "high"),
    [("30", 30.0, 30.0), ("", 60.0, 60.0), ("not a number", 60.0, 60.0)],
)
def test_retry_after_reads_seconds_a_date_or_nothing(header, low, high) -> None:
    assert low <= push.retry_after_seconds(header) <= high
    # An HTTP date is seconds from now, and never more than a day.
    soon = datetime.now(UTC) + timedelta(seconds=90)
    stamp = soon.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 80.0 <= push.retry_after_seconds(stamp) <= 95.0
    far = (datetime.now(UTC) + timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert push.retry_after_seconds(far) == push.MAX_RETRY_AFTER_S


async def test_a_refusal_raises_the_failures_and_the_threshold_drops_the_row(repo, service) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    service.answer(500)
    for attempt in range(1, FAILURE_LIMIT):
        report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
        assert (report.failed, report.error) == (1, "500")
        assert repo.push_subscriptions.rows[ENDPOINT].failures == attempt
    last = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (last.failed, last.dropped) == (0, 1)
    assert repo.push_subscriptions.rows == {}


async def test_an_unreachable_push_service_is_a_failure_and_not_an_exception(repo) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse))  # noqa: E731
    report = await send_to_person(repo, MIRA, _note(), client_factory=factory)
    assert (report.failed, report.error) == (1, "ConnectError")
    assert repo.push_subscriptions.rows[ENDPOINT].failures == 1


async def test_a_header_that_will_not_build_sends_nothing_and_costs_no_row(repo, service) -> None:
    """A wrong subject or an unreadable private key breaks every send and no
    browser, so the report carries the message and every row stays."""
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    repo.push_subscriptions.add(MIRA, SECOND_ENDPOINT, a_browser())
    repo.settings.write(SUBJECT_KEY, "owner@example.test")
    for _ in range(FAILURE_LIMIT + 1):
        report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
        assert (report.sent, report.failed, report.dropped, report.held) == (0, 0, 0, 0)
    # The message of the library, not its class alone, and no key in it.
    assert report.reason.startswith("VapidException: ")
    assert "sub" in report.reason
    assert repo.vapid_public not in report.reason
    assert service.requests == []
    assert sorted(repo.push_subscriptions.rows) == [ENDPOINT, SECOND_ENDPOINT]


async def test_a_browser_whose_keys_do_not_encrypt_is_the_only_one_that_fails(
    repo, service
) -> None:
    """``encrypt`` takes the keys of one browser, so its failure is that
    subscription's own and the next one still goes out."""
    row = repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    repo.push_subscriptions.rows[ENDPOINT] = replace(row, p256dh="not a key")
    repo.push_subscriptions.add(MIRA, SECOND_ENDPOINT, a_browser())
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.failed) == (1, 1)
    assert report.error and ": " in report.error
    assert repo.push_subscriptions.rows[ENDPOINT].failures == 1


def test_the_test_route_answers_the_message_of_a_header_that_will_not_build(
    client, repo, service, monkeypatch
) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    repo.settings.write(SUBJECT_KEY, "owner@example.test")
    monkeypatch.setattr(httpx, "AsyncClient", _client_of(service))
    answer = client.post(f"{TEST_PATH}?person={MIRA}", headers=_viewer())
    assert answer.status_code == 400
    assert answer.json()["error"].startswith("VapidException: ")
    assert service.requests == []
    assert ENDPOINT in repo.push_subscriptions.rows


def test_the_test_route_still_answers_200_when_a_browser_refused(
    client, repo, service, monkeypatch
) -> None:
    """A refusal of one push service is the answer of the send, not a 400: a
    row was touched, so the page shows the counts."""
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    service.answer(500)
    monkeypatch.setattr(httpx, "AsyncClient", _client_of(service))
    answer = client.post(f"{TEST_PATH}?person={MIRA}", headers=_viewer())
    assert answer.status_code == 200
    assert answer.json()["failed"] == 1


async def test_one_dead_browser_does_not_stop_the_other(repo, service) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    repo.push_subscriptions.add(MIRA, SECOND_ENDPOINT, a_browser())

    def handler(request: httpx.Request) -> httpx.Response:
        service.requests.append(request)
        return httpx.Response(410 if str(request.url) == ENDPOINT else 201)

    report = await send_to_person(
        repo,
        MIRA,
        _note(),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (report.sent, report.dropped) == (1, 1)
    assert list(repo.push_subscriptions.rows) == [SECOND_ENDPOINT]


async def test_a_store_that_fails_does_not_raise_into_the_turn(repo, service) -> None:
    async def boom(person_id: str) -> list[PushSubscription]:
        raise RuntimeError("the database is away")

    repo.push_subscriptions.list_for = boom
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.error) == (0, "RuntimeError")
    assert service.requests == []


# --- off by default ---------------------------------------------------------


async def test_the_sender_does_nothing_while_push_is_off(repo, service) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    repo.settings.write(ENABLED_KEY, "false")
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.reason) == (0, OFF)
    assert service.requests == []


async def test_the_sender_does_nothing_with_a_key_missing(repo, service) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    del repo.settings.rows[SUBJECT_KEY]
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert (report.sent, report.reason) == (0, push.NO_KEYS)
    assert service.requests == []


async def test_a_missing_settings_key_names_the_variable(repo, service, monkeypatch) -> None:
    repo.push_subscriptions.add(MIRA, ENDPOINT, a_browser())
    monkeypatch.delenv(SETTINGS_KEY_ENV, raising=False)
    report = await send_to_person(repo, MIRA, _note(), client_factory=service.factory())
    assert report.sent == 0
    assert SETTINGS_KEY_ENV in report.reason
    assert service.requests == []


# --- the routes -------------------------------------------------------------


def _subscription_body(browser: Browser, endpoint: str = ENDPOINT) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": browser.p256dh, "auth": browser.auth},
        "user_agent": "a browser",
    }


def test_the_routes_need_a_caller_of_the_allowlist(client) -> None:
    assert client.get(KEY_PATH).status_code == 401
    channels = {"Authorization": f"Bearer {CHANNELS}"}
    assert client.get(KEY_PATH, headers=channels).status_code == 403


def test_the_key_route_gives_the_public_key(client, repo) -> None:
    answer = client.get(KEY_PATH, headers=_viewer())
    assert answer.status_code == 200
    assert answer.json() == {"key": repo.vapid_public}


def test_a_browser_subscribes_and_unsubscribes(client, repo) -> None:
    browser = a_browser()
    url = f"{SUBSCRIBE_PATH}?person={MIRA}"
    answer = client.post(url, headers=_viewer(), json=_subscription_body(browser))
    assert answer.status_code == 200 and answer.json() == {"subscribed": True}
    row = repo.push_subscriptions.rows[ENDPOINT]
    assert (row.person_id, row.p256dh, row.user_agent) == (MIRA, browser.p256dh, "a browser")
    # The same browser again updates the one row.
    client.post(url, headers=_viewer(), json=_subscription_body(browser))
    assert list(repo.push_subscriptions.rows) == [ENDPOINT]
    gone = client.request("DELETE", url, headers=_viewer(), json={"endpoint": ENDPOINT})
    assert gone.status_code == 200 and gone.json() == {"removed": True}
    assert repo.push_subscriptions.rows == {}
    again = client.request("DELETE", url, headers=_viewer(), json={"endpoint": ENDPOINT})
    assert again.status_code == 404 and again.json()["error"] == NO_SUBSCRIPTION


def test_a_subscription_is_that_persons_alone(client, repo) -> None:
    repo.push_subscriptions.add("alex", ENDPOINT, a_browser())
    answer = client.request(
        "DELETE", f"{SUBSCRIBE_PATH}?person={MIRA}", headers=_viewer(), json={"endpoint": ENDPOINT}
    )
    assert answer.status_code == 404
    assert ENDPOINT in repo.push_subscriptions.rows


def test_a_route_that_names_no_person_or_an_unknown_one_is_refused(client) -> None:
    body = _subscription_body(a_browser())
    assert client.post(SUBSCRIBE_PATH, headers=_viewer(), json=body).status_code == 400
    unknown = client.post(f"{SUBSCRIBE_PATH}?person=nobody", headers=_viewer(), json=body)
    assert unknown.status_code == 403


def test_an_endpoint_must_be_an_https_url(client, repo) -> None:
    browser = a_browser()
    for bad in ("http://push.test/send/one", "file:///etc/passwd", "not a url"):
        answer = client.post(
            f"{SUBSCRIBE_PATH}?person={MIRA}",
            headers=_viewer(),
            json=_subscription_body(browser, endpoint=bad),
        )
        assert answer.status_code == 422, bad
    assert repo.push_subscriptions.rows == {}


def test_the_test_route_sends_one_notification(client, repo, service, monkeypatch) -> None:
    browser = a_browser()
    repo.push_subscriptions.add(MIRA, ENDPOINT, browser)
    monkeypatch.setattr(httpx, "AsyncClient", _client_of(service))
    answer = client.post(f"{TEST_PATH}?person={MIRA}", headers=_viewer())
    assert answer.status_code == 200
    assert answer.json() == {"sent": 1, "failed": 0, "dropped": 0, "held": 0, "error": ""}
    assert browser.read(service.requests[-1].content)["title"] == "Joshua"


def test_the_test_route_is_404_with_no_subscription(client) -> None:
    answer = client.post(f"{TEST_PATH}?person={MIRA}", headers=_viewer())
    assert answer.status_code == 404
    assert answer.json()["error"] == NO_SUBSCRIPTION


@pytest.mark.parametrize(
    ("verb", "path", "body"),
    [
        ("GET", KEY_PATH, None),
        ("POST", SUBSCRIBE_PATH, "subscription"),
        ("DELETE", SUBSCRIBE_PATH, {"endpoint": ENDPOINT}),
        ("POST", TEST_PATH, None),
    ],
)
def test_every_route_is_503_while_push_is_off(client, repo, verb, path, body) -> None:
    repo.settings.rows.pop(ENABLED_KEY)
    if body == "subscription":
        body = _subscription_body(a_browser())
    url = f"{path}?person={MIRA}"
    answer = client.request(verb, url, headers=_viewer(), json=body)
    assert answer.status_code == 503, path
    assert answer.json()["error"] == OFF


def test_every_route_is_503_with_a_key_missing(client, repo) -> None:
    del repo.settings.rows[PRIVATE_KEY]
    answer = client.get(KEY_PATH, headers=_viewer())
    assert answer.status_code == 503
    assert answer.json()["error"] == push.NO_KEYS


def test_every_route_is_503_with_no_settings_key(client, monkeypatch) -> None:
    monkeypatch.delenv(SETTINGS_KEY_ENV, raising=False)
    answer = client.get(KEY_PATH, headers=_viewer())
    assert answer.status_code == 503
    assert SETTINGS_KEY_ENV in answer.json()["error"]


def _client_of(service: PushService):  # type: ignore[no-untyped-def]
    """``httpx.AsyncClient`` of the fake push service, for the route tests."""
    real = httpx.AsyncClient

    def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(service.handler)
        return real(*args, **kwargs)

    return build
