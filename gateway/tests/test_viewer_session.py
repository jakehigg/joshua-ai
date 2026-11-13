"""Offline tests for the session cookie of the viewer and its sign-in routes.

A fake core stands in for the ``/auth/*`` routes through a MockTransport, so
no network and no database is needed. The tests cover the cookie itself
(issue, read, expiry, "remember me"), the attributes it carries on a
response, the stamp that a changed secret moves, and the five routes of
``viewer_api.session``.
"""

from __future__ import annotations

import json
import textwrap
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import ViewerSession, signed_in
from joshua_gateway import viewer, viewer_session
from joshua_shared import config
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

ALEX = signed_in("alex")

# The stamp core holds in these tests, and the one the cookies of ``signed_in``
# carry, which is ``conftest.VIEWER_STAMP``.
STAMP = "2026-11-01T09:00:00+00:00"
LATER = "2026-11-12T18:30:00+00:00"

CORE_URL = "http://core.test"
VIEWER_TOKEN = "viewer-token"
ORIGIN = {"Origin": "http://testserver", "Host": "testserver"}

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
    viewer:
      enabled: true
      auth:
        method: password
    """)

CONFIG_PIN = CONFIG.replace("method: password", "method: pin")


class FakeCore:
    """Core's ``/auth/*`` routes: the people who have a secret, and the stamp
    of each. ``secret`` is what ``POST /auth/verify`` accepts."""

    def __init__(self, secret: str = "open-sesame") -> None:
        self.secret = secret
        self.stamps: dict[str, str] = {"alex": STAMP, "mia": STAMP}
        self.requests: list[httpx.Request] = []
        self.unreachable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("down", request=request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == viewer_session.VERIFY_PATH:
            person, secret = body.get("person"), body.get("secret")
            ok = person in self.stamps and secret == self.secret
            return httpx.Response(200, json={"ok": ok})
        if path == viewer_session.SECRET_PATH:
            if body.get("secret") != self.secret:
                return httpx.Response(403, json={"error": "wrong secret"})
            self.secret = body.get("new_secret", "")
            self.stamps[body["person"]] = LATER
            return httpx.Response(200, json={"ok": True, "person": body["person"]})
        if path == viewer_session.PEOPLE_PATH:
            return httpx.Response(200, json={"people": sorted(self.stamps)})
        person_id = path.rsplit("/", 1)[-1]
        if person_id not in self.stamps:
            return httpx.Response(404, json={"error": "no secret"})
        return httpx.Response(200, json={"person": person_id, "updated_at": self.stamps[person_id]})

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch) -> None:
    """Empty the stamp cache, which lives for the process."""
    monkeypatch.setattr(viewer_session, "_STAMPS", {})


@pytest.fixture
def core(monkeypatch) -> FakeCore:
    """A fake core, as every ``from_env`` of the module gives it."""
    fake = FakeCore()
    transport = httpx.MockTransport(fake.handler)

    def from_env(env: Any) -> viewer_session.CoreAuth | None:
        if not env.get(viewer_session.CORE_URL_ENV) or not env.get(viewer_session.VIEWER_TOKEN_ENV):
            return None
        return viewer_session.CoreAuth(CORE_URL, VIEWER_TOKEN, transport=transport)

    monkeypatch.setenv(viewer_session.CORE_URL_ENV, CORE_URL)
    monkeypatch.setenv(viewer_session.VIEWER_TOKEN_ENV, VIEWER_TOKEN)
    monkeypatch.setattr(viewer_session, "from_env", from_env)
    return fake


def _data(monkeypatch, tmp_path: Path, text: str = CONFIG) -> Path:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    (data / "wiki/pizza.md").write_text("# Pizza\n\nDough and sauce.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(text)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return data


@pytest.fixture
def client(monkeypatch, tmp_path, core) -> TestClient:
    _data(monkeypatch, tmp_path)
    return TestClient(viewer.build_app())


def _cookie(response: Any) -> Any:
    """The session cookie of a response, parsed."""
    jar: SimpleCookie = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    return jar[viewer_session.COOKIE_NAME]


# -- the cookie itself ------------------------------------------------------


def test_a_cookie_reads_back_as_the_person_it_names() -> None:
    session = viewer_session.read(viewer_session.issue("alex", STAMP))
    assert session is not None
    assert (session.person_id, session.stamp, session.remember) == ("alex", STAMP, False)
    assert session.expires_at > viewer_session._now()


def test_a_cookie_of_another_key_is_refused(monkeypatch) -> None:
    """The signature is HMAC under ``JOSHUA_VIEWER_SESSION_SECRET``, so a
    cookie another instance signed is no cookie here."""
    monkeypatch.setenv(viewer_session.SESSION_SECRET_ENV, "first-key")
    token = viewer_session.issue("alex", STAMP)
    assert viewer_session.read(token) is not None
    monkeypatch.setenv(viewer_session.SESSION_SECRET_ENV, "second-key")
    assert viewer_session.read(token) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nodot",
        "eyJwIjoiYWxleCJ9.not-a-signature",
        "....",
    ],
)
def test_a_cookie_in_another_shape_is_refused(token: str) -> None:
    assert viewer_session.read(token) is None


def test_a_payload_that_changed_is_refused() -> None:
    """The person is signed, so swapping it breaks the signature."""
    payload, _, signature = viewer_session.issue("mia", STAMP).partition(".")
    other, _, _ = viewer_session.issue("alex", STAMP).partition(".")
    assert viewer_session.read(f"{other}.{signature}") is None


def test_a_cookie_expires(monkeypatch) -> None:
    now = 1_000_000
    monkeypatch.setattr(viewer_session, "_now", lambda: now)
    token = viewer_session.issue("alex", STAMP)
    monkeypatch.setattr(viewer_session, "_now", lambda: now + viewer_session.SESSION_MAX_AGE_S - 1)
    assert viewer_session.read(token) is not None
    monkeypatch.setattr(viewer_session, "_now", lambda: now + viewer_session.SESSION_MAX_AGE_S)
    assert viewer_session.read(token) is None


def test_remember_me_lives_a_month_and_the_browser_session_a_day(monkeypatch) -> None:
    now = 1_000_000
    monkeypatch.setattr(viewer_session, "_now", lambda: now)
    remembered = viewer_session.read(viewer_session.issue("alex", STAMP, remember=True))
    plain = viewer_session.read(viewer_session.issue("alex", STAMP))
    assert remembered is not None and plain is not None
    assert remembered.expires_at == now + viewer_session.REMEMBER_MAX_AGE_S
    assert plain.expires_at == now + viewer_session.SESSION_MAX_AGE_S
    assert remembered.remember is True and plain.remember is False
    assert viewer_session.REMEMBER_MAX_AGE_S == 30 * 24 * 60 * 60


# -- what the cookie carries on a response ----------------------------------


def test_the_attributes_of_the_cookie() -> None:
    response = JSONResponse({})
    viewer_session.set_cookie(response, "token", remember=False, secure=False)
    cookie = _cookie(response)
    assert cookie.value == "token"
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert cookie["secure"] == ""
    # No ``Max-Age``: the browser drops it when it closes.
    assert cookie["max-age"] == ""


def test_remember_me_is_the_only_cookie_with_a_max_age() -> None:
    assert viewer_session.max_age(False) is None
    assert viewer_session.max_age(True) == viewer_session.REMEMBER_MAX_AGE_S
    response = JSONResponse({})
    viewer_session.set_cookie(response, "token", remember=True, secure=True)
    cookie = _cookie(response)
    assert cookie["max-age"] == str(viewer_session.REMEMBER_MAX_AGE_S)
    assert cookie["secure"] is True


@pytest.mark.parametrize(
    ("headers", "secure"),
    [
        ({}, False),
        ({"X-Forwarded-Proto": "https"}, True),
        ({"X-Forwarded-Proto": "HTTPS"}, True),
        ({"X-Forwarded-Proto": "https, http"}, True),
        ({"X-Forwarded-Proto": "http"}, False),
    ],
)
def test_the_proxy_says_whether_the_cookie_is_secure(
    client, headers: dict[str, str], secure: bool
) -> None:
    response = client.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers={**ORIGIN, **headers},
    )
    assert response.status_code == 200
    assert ("secure" in response.headers["set-cookie"].lower()) is secure


# -- the stamp of a person's secret -----------------------------------------


async def test_the_stamp_is_cached_and_a_failure_is_not(core) -> None:
    auth = viewer_session.from_env(
        {viewer_session.CORE_URL_ENV: CORE_URL, viewer_session.VIEWER_TOKEN_ENV: VIEWER_TOKEN}
    )
    assert auth is not None
    assert await viewer_session.current_stamp(auth, "alex") == STAMP
    assert await viewer_session.current_stamp(auth, "alex") == STAMP
    # One call for the two reads: the cache answered the second.
    assert core.paths() == [f"{viewer_session.PEOPLE_PATH}/alex"]
    assert viewer_session.STAMP_TTL_S <= 60

    core.unreachable = True
    viewer_session.forget("alex")
    assert await viewer_session.current_stamp(auth, "alex") is None
    tried = len(core.paths())
    assert await viewer_session.current_stamp(auth, "alex") is None
    # A failure is never cached, so each read asks core again. A down core
    # costs the retries of the fleet client, so the count only grows.
    assert len(core.paths()) > tried


async def test_a_person_with_no_secret_has_no_stamp(core) -> None:
    auth = viewer_session.from_env(
        {viewer_session.CORE_URL_ENV: CORE_URL, viewer_session.VIEWER_TOKEN_ENV: VIEWER_TOKEN}
    )
    assert auth is not None
    assert await viewer_session.current_stamp(auth, "nobody") is None


def test_no_core_means_no_client() -> None:
    assert viewer_session.from_env({}) is None
    assert viewer_session.from_env({viewer_session.CORE_URL_ENV: CORE_URL}) is None
    assert viewer_session.from_env({viewer_session.VIEWER_TOKEN_ENV: VIEWER_TOKEN}) is None


def test_a_cookie_stands_on_its_signature_with_no_core(monkeypatch, tmp_path) -> None:
    """With no ``CORE_URL`` nobody could sign in, so nothing but this process
    could have signed the cookie: the signature and the expiry are the check."""
    monkeypatch.delenv(viewer_session.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(viewer_session.VIEWER_TOKEN_ENV, raising=False)
    _data(monkeypatch, tmp_path)
    client = TestClient(viewer.build_app())
    assert client.get("/api/v1/me", auth=ALEX).json()["person"]["id"] == "alex"


# -- a cookie that core no longer vouches for -------------------------------


def test_a_changed_secret_signs_out_every_outstanding_cookie(client, core) -> None:
    """The cookie carries the stamp of the secret, so the stamp core holds
    after a change matches no cookie that was issued before it."""
    assert client.get("/api/v1/me", auth=ALEX).status_code == 200
    core.stamps["alex"] = LATER
    viewer_session.forget("alex")
    refused = client.get("/api/v1/me", auth=ALEX)
    assert refused.status_code == 401
    assert refused.json() == {"error": "sign in to read this"}
    # A cookie that carries the stamp core holds now is the way back in.
    fresh = ViewerSession("alex", viewer_session.issue("alex", LATER))
    assert client.get("/api/v1/me", auth=fresh).status_code == 200


def test_a_cookie_core_cannot_vouch_for_is_refused(client, core) -> None:
    core.unreachable = True
    assert client.get("/api/v1/me", auth=ALEX).status_code == 401


def test_a_person_core_dropped_is_signed_out(client, core) -> None:
    del core.stamps["alex"]
    assert client.get("/api/v1/me", auth=ALEX).status_code == 401


# -- the sign-in ------------------------------------------------------------


def test_a_sign_in_answers_the_person_and_the_cookie(client) -> None:
    response = client.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "person": {"id": "alex", "name": "Alex", "role": "member"},
    }
    assert response.headers["cache-control"] == "no-store"
    token = _cookie(response).value
    session = viewer_session.read(token)
    assert session is not None and session.person_id == "alex"
    assert session.stamp == STAMP


def test_the_cookie_of_a_sign_in_reads_the_viewer(client) -> None:
    signed = TestClient(viewer.build_app())
    assert signed.get("/api/v1/me").status_code == 401
    signed.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers=ORIGIN,
    )
    # The client keeps the cookie, as a browser does.
    assert signed.get("/api/v1/me").json()["person"]["id"] == "alex"


def test_remember_me_is_the_only_sign_in_that_outlives_the_browser(client) -> None:
    body: dict[str, Any] = {"person": "alex", "secret": "open-sesame"}
    plain = client.post("/api/v1/session/sign-in", json=body, headers=ORIGIN)
    assert _cookie(plain)["max-age"] == ""
    remembered = client.post(
        "/api/v1/session/sign-in", json={**body, "remember": True}, headers=ORIGIN
    )
    cookie = _cookie(remembered)
    assert cookie["max-age"] == str(viewer_session.REMEMBER_MAX_AGE_S)
    session = viewer_session.read(cookie.value)
    assert session is not None and session.remember is True


@pytest.mark.parametrize(
    "body",
    [
        {"person": "alex", "secret": "wrong"},
        {"person": "nobody", "secret": "open-sesame"},
        {"person": "", "secret": ""},
        {"person": "alex"},
    ],
)
def test_every_refused_sign_in_says_the_same_thing(client, body: dict[str, Any]) -> None:
    from joshua_gateway.viewer_api import session as session_api

    response = client.post("/api/v1/session/sign-in", json=body, headers=ORIGIN)
    assert response.status_code == 401
    assert response.json() == {"error": session_api.SIGN_IN_FAILED}
    assert "set-cookie" not in response.headers


def test_a_sign_in_from_another_origin_is_refused(client, core) -> None:
    response = client.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers={"Origin": "http://evil.test", "Host": "testserver"},
    )
    assert response.status_code == 403
    assert core.requests == []


def test_a_sign_in_with_no_core_is_503(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(viewer_session.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(viewer_session.VIEWER_TOKEN_ENV, raising=False)
    _data(monkeypatch, tmp_path)
    client = TestClient(viewer.build_app())
    response = client.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers=ORIGIN,
    )
    assert response.status_code == 503


def test_the_secret_never_comes_back(client) -> None:
    response = client.post(
        "/api/v1/session/sign-in",
        json={"person": "alex", "secret": "open-sesame"},
        headers=ORIGIN,
    )
    assert "open-sesame" not in response.text
    assert "open-sesame" not in str(response.headers)


# -- the sign-out -----------------------------------------------------------


def test_the_sign_out_clears_the_cookie(client) -> None:
    response = client.post("/api/v1/session/sign-out", headers=ORIGIN, json={})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookie = _cookie(response)
    assert cookie.value == ""
    assert cookie["path"] == "/"


def test_the_sign_out_needs_no_session(client) -> None:
    """A cookie the viewer no longer accepts can still be thrown away."""
    assert client.post("/api/v1/session/sign-out", headers=ORIGIN, json={}).status_code == 200


def test_a_sign_out_from_another_origin_is_refused(client) -> None:
    response = client.post(
        "/api/v1/session/sign-out",
        json={},
        headers={"Origin": "http://evil.test", "Host": "testserver"},
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


# -- a person changes their own secret --------------------------------------


def test_a_change_of_secret_re_issues_the_cookie_and_signs_the_rest_out(client, core) -> None:
    response = client.post(
        "/api/v1/session/secret",
        json={"secret": "open-sesame", "new_secret": "new-one"},
        headers=ORIGIN,
        auth=ALEX,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    token = _cookie(response).value
    session = viewer_session.read(token)
    assert session is not None and session.stamp == LATER
    # The old cookie carries the old stamp, so every other browser is out,
    # and the one the answer carried reads on.
    assert client.get("/api/v1/me", auth=ALEX).status_code == 401
    assert client.get("/api/v1/me", auth=ViewerSession("alex", token)).status_code == 200
    assert core.secret == "new-one"


def test_a_change_keeps_remember_me(client) -> None:
    remembered = signed_in("alex", remember=True)
    response = client.post(
        "/api/v1/session/secret",
        json={"secret": "open-sesame", "new_secret": "new-one"},
        headers=ORIGIN,
        auth=remembered,
    )
    cookie = _cookie(response)
    assert cookie["max-age"] == str(viewer_session.REMEMBER_MAX_AGE_S)


def test_a_wrong_current_secret_changes_nothing(client, core) -> None:
    response = client.post(
        "/api/v1/session/secret",
        json={"secret": "wrong", "new_secret": "new-one"},
        headers=ORIGIN,
        auth=ALEX,
    )
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert core.secret == "open-sesame"


def test_a_change_needs_both_secrets(client) -> None:
    response = client.post(
        "/api/v1/session/secret",
        json={"secret": "open-sesame", "new_secret": ""},
        headers=ORIGIN,
        auth=ALEX,
    )
    assert response.status_code == 400


def test_a_change_needs_a_session(client) -> None:
    response = client.post(
        "/api/v1/session/secret",
        json={"secret": "open-sesame", "new_secret": "new-one"},
        headers=ORIGIN,
    )
    assert response.status_code == 401


# -- what the sign-in page reads --------------------------------------------


def test_the_method_of_the_instance_needs_no_session(client) -> None:
    response = client.get("/api/v1/auth-method")
    assert response.status_code == 200
    assert response.json() == {"method": "password", "anonymous": False}
    assert response.headers["cache-control"] == "no-store"


def test_the_pin_method_lists_the_people_the_roster_names(monkeypatch, tmp_path, core) -> None:
    _data(monkeypatch, tmp_path, CONFIG_PIN)
    client = TestClient(viewer.build_app())
    core.stamps["ghost"] = STAMP  # core knows them, the roster does not
    response = client.get("/api/v1/sign-in/people")
    assert response.status_code == 200
    assert response.json() == {
        "people": [
            {"id": "alex", "name": "Alex", "role": "member"},
            {"id": "mia", "name": "Mia", "role": "guest"},
        ]
    }


def test_the_list_of_people_is_404_for_any_other_method(client) -> None:
    assert client.get("/api/v1/sign-in/people").status_code == 404


def test_the_list_of_people_with_no_core_is_503(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(viewer_session.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(viewer_session.VIEWER_TOKEN_ENV, raising=False)
    _data(monkeypatch, tmp_path, CONFIG_PIN)
    client = TestClient(viewer.build_app())
    assert client.get("/api/v1/sign-in/people").status_code == 503
