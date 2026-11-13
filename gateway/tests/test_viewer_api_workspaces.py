"""Offline tests for the workspaces routes of the viewer.

Core is a ``httpx.MockTransport``. The tests cover the proxy of each route
(path, method, person, body), the answer that passes through, a guest (403), a
cross-site write (403), a missing core (503), and the sign-in.
"""

from __future__ import annotations

import json
import textwrap

import httpx
import pytest
from conftest import bad_cookie, core_stamp, signed_in
from joshua_gateway import viewer
from joshua_gateway.viewer_api import workspaces
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = signed_in("alex")
GUS = signed_in("gus")
SAM = signed_in("sam")
BASE = "/api/v1/workspaces"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: gus
        name: Gus
        role: guest
      - id: sam
        name: Sam
        role: owner
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        gus: ${VIEWER_PW_GUS:-}
        sam: ${VIEWER_PW_SAM:-}
    """)


class Core:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = httpx.Response(200, json={"workspaces": []})
        self.unreachable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("no route")
        self.requests.append(request)
        return self.answer


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
    real = workspaces.FleetClient

    def fleet(base, token, timeout=15.0, **kw):
        return real(base, token, timeout, transport=httpx.MockTransport(core.handler))

    monkeypatch.setattr(workspaces, "FleetClient", fleet)
    return TestClient(viewer.build_app())


def test_each_route_proxies_to_core_with_the_person_and_the_token(client, core) -> None:
    item = "11111111-1111-1111-1111-111111111111"
    calls = [
        ("get", BASE, "GET", "/v1/workspaces", None),
        ("post", BASE, "POST", "/v1/workspaces", {"title": "Solar"}),
        ("get", BASE + "/options", "GET", "/v1/workspaces/options", None),
        ("get", BASE + "/solar", "GET", "/v1/workspaces/solar", None),
        ("patch", BASE + "/solar", "PATCH", "/v1/workspaces/solar", {"goal": "g"}),
        ("get", BASE + "/solar/items", "GET", "/v1/workspaces/solar/items", None),
        (
            "post",
            BASE + "/solar/items",
            "POST",
            "/v1/workspaces/solar/items",
            {"kind": "link", "ref": "https://x.test"},
        ),
        (
            "patch",
            f"{BASE}/solar/items/{item}",
            "PATCH",
            f"/v1/workspaces/solar/items/{item}",
            {"pinned": True},
        ),
        (
            "delete",
            f"{BASE}/solar/items/{item}",
            "DELETE",
            f"/v1/workspaces/solar/items/{item}",
            None,
        ),
    ]
    for verb, url, method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, verb)(url, auth=ALEX, **kwargs)
        assert response.status_code == 200, url
        request = core.requests[-1]
        assert (request.method, request.url.path) == (method, path)
        assert request.url.params["person"] == "alex"
        assert request.headers["authorization"] == "Bearer viewer-token"
        assert (json.loads(request.content) if request.content else None) == body
    assert len(core.requests) == len(calls)


def test_core_status_and_body_pass_through_and_are_not_cached(client, core) -> None:
    core.answer = httpx.Response(422, json={"error": "invalid request", "detail": "x"})
    response = client.post(BASE, json={"title": ""}, auth=ALEX)
    assert response.status_code == 422 and response.json()["detail"] == "x"
    assert response.headers["cache-control"] == "no-store"
    core.answer = httpx.Response(404, json={"error": "not found"})
    assert client.get(BASE + "/nope", auth=ALEX).status_code == 404


def test_the_status_filter_of_the_list_goes_to_core(client, core) -> None:
    client.get(BASE + "?status=paused", auth=ALEX)
    assert dict(core.requests[0].url.params) == {"person": "alex", "status": "paused"}


def test_an_owner_reaches_core_as_a_member_does(client, core) -> None:
    """The owner role passes the member gate, and core hears the owner's id."""
    assert client.get(BASE, auth=SAM).status_code == 200
    assert core.requests[-1].url.params["person"] == "sam"


def test_a_guest_is_403_on_every_route_and_core_hears_nothing(client, core) -> None:
    for verb, url in [
        ("get", BASE),
        ("post", BASE),
        ("get", BASE + "/options"),
        ("patch", BASE + "/a"),
        ("delete", BASE + "/a/items/b"),
    ]:
        assert getattr(client, verb)(url, auth=GUS).status_code == 403, url
    assert core.requests == []


def test_a_cross_site_write_is_403_but_a_read_is_not_checked(client, core) -> None:
    evil = {"Origin": "https://evil.example"}
    for verb, url in [
        ("post", BASE),
        ("patch", BASE + "/a"),
        ("post", BASE + "/a/items"),
        ("patch", BASE + "/a/items/b"),
        ("delete", BASE + "/a/items/b"),
    ]:
        response = client.request(verb.upper(), url, json={}, auth=ALEX, headers=evil)
        assert response.status_code == 403, url
    assert core.requests == []
    same = client.post(
        BASE, json={"title": "x"}, auth=ALEX, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 200


def test_a_body_that_is_not_an_object_is_422(client, core) -> None:
    assert client.post(BASE, content=b"[1]", auth=ALEX).status_code == 422
    assert client.post(BASE, content=b"nope", auth=ALEX).status_code == 422
    assert core.requests == []


def test_an_unreachable_or_missing_core_is_503(client, core, monkeypatch) -> None:
    core.unreachable = True
    assert client.get(BASE, auth=ALEX).status_code == 503
    monkeypatch.delenv("CORE_URL")
    assert client.get(BASE, auth=ALEX).status_code == 503


def test_the_routes_need_the_sign_in(client, core) -> None:
    assert client.get(BASE, auth=bad_cookie()).status_code == 401
    assert client.post(BASE, json={}, auth=bad_cookie()).status_code == 401
    assert core.requests == []
