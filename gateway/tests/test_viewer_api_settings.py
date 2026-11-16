"""Offline tests for the Instance routes of the viewer: the settings.

Core is a ``httpx.MockTransport``. The tests cover the two routes for an
owner (the call to core and the answer), the 403 of a member and of a guest,
a cross-site write, the sign-in, and that no answer and no log line carries
the value the owner typed for a secret.
"""

from __future__ import annotations

import json
import textwrap

import httpx
import pytest
from conftest import bad_cookie, core_stamp, signed_in
from joshua_gateway import viewer
from joshua_gateway.viewer_api import admin
from joshua_shared import config
from starlette.testclient import TestClient

SAM = signed_in("sam")
ALEX = signed_in("alex")
GUS = signed_in("gus")
BASE = "/api/v1/admin/settings"
PRIVATE = "push.vapid_private_key"
SECRET_VALUE = "a private key nobody may read back"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: sam
        name: Sam
        role: owner
      - id: alex
        name: Alex
      - id: gus
        name: Gus
        role: guest
    viewer:
      enabled: true
      users:
        sam: ${VIEWER_PW_SAM:-}
        alex: ${VIEWER_PW_ALEX:-}
        gus: ${VIEWER_PW_GUS:-}
    """)

# The two routes, as a verb, a URL and a body. The read carries none.
ROUTES = (
    ("get", BASE, None),
    ("post", BASE, {"key": "push.vapid_public_key", "value": "a-public-key"}),
)

# One group, as core answers it: the value of the secret is never in it.
GROUPS = {
    "groups": [
        {
            "prefix": "push",
            "label": "Push notifications",
            "settings": [
                {
                    "key": "push.vapid_public_key",
                    "label": "VAPID public key",
                    "hint": "",
                    "is_secret": False,
                    "is_set": True,
                    "value": "a-public-key",
                    "updated_at": "2026-11-15T10:00:00+00:00",
                    "updated_by": "sam",
                },
                {
                    "key": PRIVATE,
                    "label": "VAPID private key",
                    "hint": "",
                    "is_secret": True,
                    "is_set": True,
                    "value": None,
                    "updated_at": "2026-11-15T10:00:00+00:00",
                    "updated_by": "sam",
                },
            ],
        }
    ]
}


class Core:
    """Core's answer to each settings path the viewer calls here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, httpx.Response] = {}

    def answer(self, path: str, response: httpx.Response) -> None:
        self.answers[path] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.answers:
            return self.answers[path]
        if path == "/admin/settings" and request.method == "GET":
            return httpx.Response(200, json=GROUPS)
        if path == "/admin/settings":
            return httpx.Response(200, json={"setting": GROUPS["groups"][0]["settings"][0]})
        return httpx.Response(404, json={"error": "not found"})  # pragma: no cover


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
    real = admin.FleetClient

    def fleet(base, token, timeout=15.0, **kw):
        return real(base, token, timeout, transport=httpx.MockTransport(core.handler))

    monkeypatch.setattr(admin, "FleetClient", fleet)
    return TestClient(viewer.build_app())


def _call(client: TestClient, verb: str, url: str, body, auth):
    kwargs = {"json": body} if body is not None else {}
    return client.request(verb.upper(), url, auth=auth, **kwargs)


def test_the_owner_reads_the_groups_and_core_hears_the_prefix(client, core) -> None:
    response = client.get(f"{BASE}?prefix=push", auth=SAM)
    assert response.status_code == 200
    assert response.json() == GROUPS
    assert response.headers["cache-control"] == "no-store"
    request = core.requests[-1]
    assert (request.method, request.url.path, request.url.query) == (
        "GET",
        "/admin/settings",
        b"prefix=push",
    )
    assert request.headers["authorization"] == "Bearer viewer-token"
    # With no prefix the viewer asks for every group.
    assert client.get(BASE, auth=SAM).status_code == 200
    assert core.requests[-1].url.query == b""


def test_a_write_names_the_person_who_signed_in(client, core) -> None:
    response = client.post(BASE, json={"key": PRIVATE, "value": SECRET_VALUE}, auth=SAM)
    assert response.status_code == 200
    assert json.loads(core.requests[-1].content) == {
        "key": PRIVATE,
        "value": SECRET_VALUE,
        "person": "sam",
    }


def test_core_status_and_body_pass_through(client, core) -> None:
    """The 503 of a secret write with no key is what the page shows."""
    refusal = {"error": "JOSHUA_SETTINGS_KEY is not set: a secret setting cannot be written"}
    core.answer("/admin/settings", httpx.Response(503, json=refusal))
    response = client.post(BASE, json={"key": PRIVATE, "value": SECRET_VALUE}, auth=SAM)
    assert response.status_code == 503
    assert response.json() == refusal


def test_a_write_with_no_key_is_400_and_core_hears_nothing(client, core) -> None:
    response = client.post(BASE, json={"key": " ", "value": "x"}, auth=SAM)
    assert response.status_code == 400
    assert response.json()["error"] == admin.NO_KEY
    assert client.post(BASE, content=b"[1]", auth=SAM).status_code == 422
    assert core.requests == []


def test_a_member_and_a_guest_are_403_and_core_hears_nothing(client, core) -> None:
    for verb, url, body in ROUTES:
        for who in (ALEX, GUS):
            response = _call(client, verb, url, body, who)
            assert response.status_code == 403, url
            assert response.json()["error"] == admin.FORBIDDEN_SETTINGS
    assert core.requests == []


def test_a_cross_site_write_is_403_but_a_read_is_not_checked(client, core) -> None:
    evil = {"Origin": "https://evil.example"}
    response = client.post(BASE, json=ROUTES[1][2], auth=SAM, headers=evil)
    assert response.status_code == 403
    assert response.json()["error"] == admin.CROSS_SITE
    assert core.requests == []
    assert client.get(BASE, auth=SAM, headers=evil).status_code == 200


def test_the_routes_need_the_sign_in(client, core) -> None:
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, bad_cookie()).status_code == 401, url
    assert core.requests == []


def test_an_unreachable_core_is_503(client, core, monkeypatch) -> None:
    monkeypatch.delenv("CORE_URL")
    response = client.get(BASE, auth=SAM)
    assert response.status_code == 503
    assert response.json()["error"] == admin.NOT_CONFIGURED
    assert core.requests == []


def test_no_answer_and_no_log_line_carries_the_secret(client, core, caplog) -> None:
    with caplog.at_level("INFO"):
        response = client.post(BASE, json={"key": PRIVATE, "value": SECRET_VALUE}, auth=SAM)
    assert response.status_code == 200
    assert SECRET_VALUE not in response.text
    assert SECRET_VALUE not in caplog.text
