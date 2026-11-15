"""Offline tests for the push routes of the viewer: they proxy core.

Core is a ``httpx.MockTransport``, so no push service and no core is reached.
The tests cover the four routes, that each one names the person who signed in
and never a person from a body, the ``User-Agent`` the viewer adds to a
subscription, the cross-site write, the sign-in, and the 503 of core while push
is off, which passes through as it is.
"""

from __future__ import annotations

import json
import textwrap

import httpx
import pytest
from conftest import bad_cookie, core_stamp, signed_in
from joshua_gateway import viewer
from joshua_gateway.viewer_api import push
from joshua_shared import config
from starlette.testclient import TestClient

SAM = signed_in("sam")
ALEX = signed_in("alex")
BASE = "/api/v1/push"
ENDPOINT = "https://push.test/send/one"
OFF = "push notifications are off: the owner turns them on in Admin → Instance"

SUBSCRIPTION = {
    "endpoint": ENDPOINT,
    "expirationTime": None,
    "keys": {"p256dh": "a-p256dh", "auth": "an-auth"},
}

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: sam
        name: Sam
        role: owner
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        sam: ${VIEWER_PW_SAM:-}
        alex: ${VIEWER_PW_ALEX:-}
    """)

# The four routes, as a verb, a URL and a body.
ROUTES = (
    ("get", f"{BASE}/key", None),
    ("post", f"{BASE}/subscribe", SUBSCRIPTION),
    ("delete", f"{BASE}/subscribe", {"endpoint": ENDPOINT}),
    ("post", f"{BASE}/test", {}),
)


class Core:
    """Core's answer to each push path the viewer calls here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.response: httpx.Response | None = None

    def answer(self, response: httpx.Response) -> None:
        self.response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.response is not None:
            return self.response
        if request.url.path == "/v1/push/key":
            return httpx.Response(200, json={"key": "a-public-key"})
        if request.url.path == "/v1/push/test":
            return httpx.Response(
                200, json={"sent": 1, "failed": 0, "dropped": 0, "held": 0, "error": ""}
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"removed": True})
        return httpx.Response(200, json={"subscribed": True})


@pytest.fixture
def core() -> Core:
    return Core()


@pytest.fixture
def client(monkeypatch, tmp_path, core) -> TestClient:
    (tmp_path / "data/wiki").mkdir(parents=True)
    cfg = tmp_path / "joshua.yaml"
    cfg.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORE_URL", "http://core.test")
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", "viewer-token")
    core_stamp(monkeypatch)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    real = push.FleetClient

    def fleet(base, token, timeout=15.0, **kw):
        return real(base, token, timeout, transport=httpx.MockTransport(core.handler))

    monkeypatch.setattr(push, "FleetClient", fleet)
    return TestClient(viewer.build_app())


def _call(client: TestClient, verb: str, url: str, body, auth, **kwargs):
    if body is not None:
        kwargs["json"] = body
    return client.request(verb.upper(), url, auth=auth, **kwargs)


def test_the_key_route_passes_core_s_answer_through(client, core) -> None:
    response = client.get(f"{BASE}/key", auth=SAM)
    assert response.status_code == 200
    assert response.json() == {"key": "a-public-key"}
    assert response.headers["cache-control"] == "no-store"
    request = core.requests[-1]
    assert (request.method, request.url.path, request.url.query) == (
        "GET",
        "/v1/push/key",
        b"person=sam",
    )
    assert request.headers["authorization"] == "Bearer viewer-token"


def test_a_subscription_goes_to_core_with_the_user_agent(client, core) -> None:
    response = client.post(
        f"{BASE}/subscribe", json=SUBSCRIPTION, auth=SAM, headers={"User-Agent": "a phone"}
    )
    assert response.status_code == 200 and response.json() == {"subscribed": True}
    request = core.requests[-1]
    assert request.url.query == b"person=sam"
    # ``expirationTime`` is not core's to take, and the person is never in the body.
    assert json.loads(request.content) == {
        "endpoint": ENDPOINT,
        "keys": SUBSCRIPTION["keys"],
        "user_agent": "a phone",
    }


def test_a_delete_names_the_endpoint_and_the_person_who_signed_in(client, core) -> None:
    response = client.request(
        "DELETE", f"{BASE}/subscribe", json={"endpoint": ENDPOINT, "person": "alex"}, auth=ALEX
    )
    assert response.status_code == 200
    request = core.requests[-1]
    assert (request.method, request.url.query) == ("DELETE", b"person=alex")
    assert json.loads(request.content) == {"endpoint": ENDPOINT}


def test_the_test_route_asks_core_to_send(client, core) -> None:
    response = client.post(f"{BASE}/test", auth=SAM)
    assert response.status_code == 200
    assert response.json()["sent"] == 1
    assert core.requests[-1].url.path == "/v1/push/test"


def test_core_s_503_passes_through_while_push_is_off(client, core) -> None:
    core.answer(httpx.Response(503, json={"error": OFF}))
    for verb, url, body in ROUTES:
        response = _call(client, verb, url, body, SAM)
        assert response.status_code == 503, url
        assert response.json()["error"] == OFF


def test_a_cross_site_write_is_403_and_core_hears_nothing(client, core) -> None:
    evil = {"Origin": "https://evil.example"}
    for verb, url, body in ROUTES[1:]:
        response = _call(client, verb, url, body, SAM, headers=evil)
        assert response.status_code == 403, url
        assert response.json()["error"] == push.CROSS_SITE
    assert core.requests == []
    # A read is not checked, as elsewhere in the viewer.
    assert client.get(f"{BASE}/key", auth=SAM, headers=evil).status_code == 200


def test_a_body_that_is_not_an_object_is_422(client, core) -> None:
    assert client.post(f"{BASE}/subscribe", content=b"[1]", auth=SAM).status_code == 422
    assert client.request("DELETE", f"{BASE}/subscribe", content=b"x", auth=SAM).status_code == 422
    assert core.requests == []


def test_the_routes_need_the_sign_in(client, core) -> None:
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, bad_cookie()).status_code == 401, url
    assert core.requests == []


def test_an_unreachable_core_is_503(client, core, monkeypatch) -> None:
    monkeypatch.delenv("CORE_URL")
    response = client.get(f"{BASE}/key", auth=SAM)
    assert response.status_code == 503
    assert response.json()["error"] == push.NOT_CONFIGURED
    assert core.requests == []
