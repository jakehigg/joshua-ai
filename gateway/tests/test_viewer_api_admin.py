"""Offline tests for the Users routes of the viewer: the roster of the owner.

Core is a ``httpx.MockTransport``. The tests cover each of the five routes for
an owner (the call to core, and the answer), the 403 of a member and of a
guest on every one of them, a cross-site write, a bad body, a missing or
unreachable core, and the sign-in.
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
BASE = "/api/v1/admin/users"

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

# Every route, as a verb, a URL and a body. The reads carry none.
ROUTES = (
    ("get", BASE, None),
    ("post", BASE, {"id": "kim", "name": "Kim", "handle": "telegram:5", "secret": "pin-1234"}),
    ("post", f"{BASE}/alex/secret", {"secret": "pin-9876"}),
    ("post", f"{BASE}/alex/role", {"role": "guest"}),
    ("post", f"{BASE}/alex/remove", None),
)


class Core:
    """Core's answer to each path the viewer calls here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.people = ["sam", "alex"]
        self.answers: dict[str, httpx.Response] = {}
        self.unreachable = False

    def answer(self, path: str, response: httpx.Response) -> None:
        self.answers[path] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("no route")
        self.requests.append(request)
        path = request.url.path
        if path in self.answers:
            return self.answers[path]
        if path == "/auth/people":
            return httpx.Response(200, json={"people": self.people})
        if path == "/auth/secret/admin":
            return httpx.Response(200, json={"ok": True, "person": "alex"})
        if path == "/admin/people":
            return httpx.Response(201, json={"person": {"id": "kim", "name": "Kim"}})
        if path.endswith("/role"):
            return httpx.Response(200, json={"person": {"id": "alex", "role": "guest"}})
        if path.endswith("/remove"):
            return httpx.Response(200, json={"removed": True, "id": "alex"})
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


def test_the_owner_reaches_core_on_every_route(client, core) -> None:
    """Each route calls core with its own method, path and body, and the
    fleet token of the viewer. The add makes the two calls."""
    calls = [
        ("get", BASE, None, [("GET", "/auth/people", None)]),
        (
            "post",
            BASE,
            {"id": "kim", "name": "Kim", "handle": "telegram:5", "secret": "pin-1234"},
            [
                (
                    "POST",
                    "/admin/people",
                    {"id": "kim", "name": "Kim", "handle": "telegram:5", "role": "member"},
                ),
                ("POST", "/auth/secret/admin", {"person": "kim", "new_secret": "pin-1234"}),
            ],
        ),
        (
            "post",
            f"{BASE}/alex/secret",
            {"secret": "pin-9876"},
            [("POST", "/auth/secret/admin", {"person": "alex", "new_secret": "pin-9876"})],
        ),
        (
            "post",
            f"{BASE}/alex/role",
            {"role": "guest"},
            [("POST", "/admin/people/alex/role", {"role": "guest"})],
        ),
        ("post", f"{BASE}/alex/remove", None, [("POST", "/admin/people/alex/remove", None)]),
    ]
    for verb, url, body, expected in calls:
        core.requests.clear()
        response = _call(client, verb, url, body, SAM)
        assert response.status_code in (200, 201), url
        assert response.headers["cache-control"] == "no-store"
        seen = [
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
            for request in core.requests
        ]
        assert seen == expected, url
        assert all(r.headers["authorization"] == "Bearer viewer-token" for r in core.requests)


def test_the_roster_joins_the_config_with_who_has_a_secret(client, core) -> None:
    body = client.get(BASE, auth=SAM).json()
    assert body["users"] == [
        {"id": "sam", "name": "Sam", "role": "owner", "has_secret": True},
        {"id": "alex", "name": "Alex", "role": "member", "has_secret": True},
        {"id": "gus", "name": "Gus", "role": "guest", "has_secret": False},
    ]


def test_a_person_core_names_who_is_not_on_the_roster_is_left_out(client, core) -> None:
    core.people = ["sam", "ghost"]
    ids = [user["id"] for user in client.get(BASE, auth=SAM).json()["users"]]
    assert ids == ["sam", "alex", "gus"]


def test_the_add_says_whether_the_secret_was_set(client, core) -> None:
    body = {"id": "kim", "name": "Kim", "handle": "telegram:5", "secret": "pin-1234"}
    response = client.post(BASE, json=body, auth=SAM)
    assert response.status_code == 201
    assert response.json() == {"person": {"id": "kim", "name": "Kim"}, "secret_set": True}


def test_an_add_with_no_secret_adds_the_person_alone(client, core) -> None:
    """A person with no secret cannot sign in, and the owner can set one later."""
    response = client.post(
        BASE, json={"id": "kim", "name": "Kim", "handle": "telegram:5"}, auth=SAM
    )
    assert response.status_code == 201
    assert response.json()["secret_set"] is False
    assert [request.url.path for request in core.requests] == ["/admin/people"]


def test_a_secret_core_refuses_leaves_the_person_added(client, core) -> None:
    core.answer("/auth/secret/admin", httpx.Response(404, json={"error": "unknown person"}))
    body = {"id": "kim", "name": "Kim", "handle": "telegram:5", "secret": "pin-1234"}
    response = client.post(BASE, json=body, auth=SAM)
    assert response.status_code == 404
    assert response.json()["secret_set"] is False
    assert response.json()["error"] == admin.SECRET_FAILED
    assert response.json()["person"] == {"id": "kim", "name": "Kim"}


def test_the_role_the_owner_names_goes_to_core_and_the_answer_passes_through(client, core) -> None:
    response = client.post(f"{BASE}/alex/role", json={"role": "owner"}, auth=SAM)
    assert response.status_code == 200
    assert response.json() == {"person": {"id": "alex", "role": "guest"}}
    assert json.loads(core.requests[-1].content) == {"role": "owner"}


def test_core_status_and_body_pass_through(client, core) -> None:
    core.answer("/admin/people", httpx.Response(400, json={"error": "name is required"}))
    response = client.post(BASE, json={"id": "kim", "handle": "telegram:5"}, auth=SAM)
    assert response.status_code == 400
    assert response.json() == {"error": "name is required"}
    core.answer(
        "/admin/people/nope/remove", httpx.Response(404, json={"error": "unknown person nope"})
    )
    assert client.post(f"{BASE}/nope/remove", auth=SAM).status_code == 404


def test_a_member_is_403_on_every_route_and_core_hears_nothing(client, core) -> None:
    for verb, url, body in ROUTES:
        response = _call(client, verb, url, body, ALEX)
        assert response.status_code == 403, url
        assert response.json()["error"] == admin.FORBIDDEN
    assert core.requests == []


def test_a_guest_is_403_on_every_route_and_core_hears_nothing(client, core) -> None:
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, GUS).status_code == 403, url
    assert core.requests == []


def test_the_owner_is_200_on_every_route(client, core) -> None:
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, SAM).status_code in (200, 201), url


def test_a_cross_site_write_is_403_but_a_read_is_not_checked(client, core) -> None:
    evil = {"Origin": "https://evil.example"}
    for verb, url, body in ROUTES[1:]:
        response = client.request(verb.upper(), url, json=body or {}, auth=SAM, headers=evil)
        assert response.status_code == 403, url
        assert response.json()["error"] == admin.CROSS_SITE
    assert core.requests == []
    assert client.get(BASE, auth=SAM, headers=evil).status_code == 200


def test_a_body_that_is_not_an_object_is_422_and_an_empty_secret_is_400(client, core) -> None:
    assert client.post(BASE, content=b"[1]", auth=SAM).status_code == 422
    assert client.post(f"{BASE}/alex/role", content=b"nope", auth=SAM).status_code == 422
    assert client.post(f"{BASE}/alex/secret", json={"secret": ""}, auth=SAM).status_code == 400
    assert core.requests == []


def test_an_unreachable_or_missing_core_is_503(client, core, monkeypatch) -> None:
    core.unreachable = True
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, SAM).status_code == 503, url
    core.unreachable = False
    monkeypatch.delenv("CORE_URL")
    response = client.get(BASE, auth=SAM)
    assert response.status_code == 503
    assert response.json()["error"] == admin.NOT_CONFIGURED
    assert core.requests == []


def test_an_answer_from_core_in_an_unknown_shape_is_503(client, core) -> None:
    """The page never says "nobody can sign in" because core said nothing."""
    core.answer("/auth/people", httpx.Response(200, json={"people": "sam"}))
    response = client.get(BASE, auth=SAM)
    assert response.status_code == 503
    assert response.json()["error"] == admin.UNREACHABLE
    core.answer("/auth/people", httpx.Response(200, content=b"not json"))
    assert client.get(BASE, auth=SAM).status_code == 503


def test_the_routes_need_the_sign_in(client, core) -> None:
    for verb, url, body in ROUTES:
        assert _call(client, verb, url, body, bad_cookie()).status_code == 401, url
    assert core.requests == []


def test_the_anonymous_person_is_not_the_owner(monkeypatch, tmp_path, core) -> None:
    """An open viewer serves a request with no cookie as a member, and a
    member is not the owner, so the roster is 403 there too."""
    (tmp_path / "data/wiki").mkdir(parents=True)
    cfg = tmp_path / "joshua.yaml"
    cfg.write_text(CONFIG.replace("  enabled: true\n", "  enabled: true\n  anonymous: true\n"))
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    open_client = TestClient(viewer.build_app())
    for verb, url, body in ROUTES:
        assert _call(open_client, verb, url, body, None).status_code == 403, url
    assert core.requests == []


def test_no_answer_and_no_log_line_carries_a_secret(client, core, caplog) -> None:
    secret = "pin-1234"
    with caplog.at_level("INFO"):
        response = client.post(f"{BASE}/alex/secret", json={"secret": secret}, auth=SAM)
    assert response.status_code == 200
    assert secret not in response.text
    assert secret not in caplog.text
