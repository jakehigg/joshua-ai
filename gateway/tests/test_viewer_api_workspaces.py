"""Offline tests for the workspaces routes of the viewer.

Core is a ``httpx.MockTransport``. The tests cover the proxy of each route
(path, method, person, body), the answer that passes through, a guest (403), a
cross-site write (403), a missing core (503), and the sign-in.

The last section covers the charm beside the name of a workspace, which the
viewer composes from core's answer and from the assignments manager.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

import httpx
import pytest
from conftest import bad_cookie, core_stamp, signed_in
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
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
        ("post", BASE + "/solar/read", "POST", "/v1/workspaces/solar/read", {"turn_id": 12}),
        ("get", BASE + "/solar/notify", "GET", "/v1/workspaces/solar/notify", None),
        ("post", BASE + "/solar/notify", "POST", "/v1/workspaces/solar/notify", {"mode": "all"}),
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


def test_a_two_segment_ref_reaches_core_whole(client, core) -> None:
    """``shared/<slug>`` and ``<owner>/<slug>`` come URL-encoded from the
    browser and go to core as two segments, on every route of one workspace."""
    item = "11111111-1111-1111-1111-111111111111"
    calls = [
        ("get", f"{BASE}/shared%2Fthanksgiving", "GET", "/v1/workspaces/shared/thanksgiving"),
        ("patch", f"{BASE}/shared%2Fthanksgiving", "PATCH", "/v1/workspaces/shared/thanksgiving"),
        (
            "get",
            f"{BASE}/alex%2Fsolar/items",
            "GET",
            "/v1/workspaces/alex/solar/items",
        ),
        (
            "post",
            f"{BASE}/alex%2Fsolar/items",
            "POST",
            "/v1/workspaces/alex/solar/items",
        ),
        (
            "patch",
            f"{BASE}/alex%2Fsolar/items/{item}",
            "PATCH",
            f"/v1/workspaces/alex/solar/items/{item}",
        ),
        (
            "delete",
            f"{BASE}/alex%2Fsolar/items/{item}",
            "DELETE",
            f"/v1/workspaces/alex/solar/items/{item}",
        ),
        (
            "post",
            f"{BASE}/shared%2Fthanksgiving/read",
            "POST",
            "/v1/workspaces/shared/thanksgiving/read",
        ),
        (
            "post",
            f"{BASE}/shared%2Fthanksgiving/notify",
            "POST",
            "/v1/workspaces/shared/thanksgiving/notify",
        ),
    ]
    for verb, url, method, path in calls:
        response = client.request(verb.upper(), url, json={}, auth=ALEX)
        assert response.status_code == 200, url
        request = core.requests[-1]
        assert (request.method, request.url.path) == (method, path), url
        assert request.url.params["person"] == "alex"
    # The options route is a name, never a ref.
    assert client.get(BASE + "/options", auth=ALEX).status_code == 200
    assert core.requests[-1].url.path == "/v1/workspaces/options"


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
        ("post", BASE + "/a/read"),
        ("get", BASE + "/a/notify"),
        ("post", BASE + "/a/notify"),
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
        ("post", BASE + "/a/read"),
        ("post", BASE + "/a/notify"),
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


# --- the charm beside the name ----------------------------------------------
#
# The list and the read carry ``charm``: Joshua is working for the workspace,
# something of it waits for a person, or neither and there is none. The
# sources are core's ``turn_in_flight`` and the assignments manager, which is
# another MockTransport here.

WS = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def row(workspace: str = WS, **fields: Any) -> dict[str, Any]:
    return {"id": workspace, "slug": "solar", "title": "Solar install", "shared": False, **fields}


def assignment(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 31,
        "title": "RCA: the receipts",
        "status": "running",
        "state": "in_progress",
        "workspace": WS,
    }
    return base | fields


def manager(
    monkeypatch,
    *,
    draft: list[dict[str, Any]] | None = None,
    in_progress: list[dict[str, Any]] | None = None,
    down: bool = False,
) -> None:
    """The assignments manager of the charm: its rows by state, or no answer."""
    rows = {"draft": draft or [], "in_progress": in_progress or []}

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, json={"items": rows.get(request.url.params.get("state"), [])})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", "viewer-token", transport=transport),
    )


def charms(client, core) -> dict[str, Any]:
    answer = client.get(BASE, auth=ALEX)
    assert answer.status_code == 200
    return {w["id"]: w["charm"] for w in answer.json()["workspaces"]}


def test_a_workspace_with_a_worker_in_flight_carries_the_working_charm(
    client, core, monkeypatch
) -> None:
    """A turn of the thread that has not finished, and an assignment of
    another workspace that is running."""
    core.answer = httpx.Response(
        200, json={"workspaces": [row(turn_in_flight=True), row(OTHER, title="Trip")]}
    )
    manager(monkeypatch, in_progress=[assignment(workspace=OTHER)])
    marks = charms(client, core)
    assert marks[WS] == {"state": "working", "reason": "Joshua is answering in this workspace"}
    assert marks[OTHER] == {"state": "working", "reason": "assignment 31 is running"}


def test_a_workspace_that_needs_a_person_carries_the_waiting_charm_and_the_reason(
    client, core, monkeypatch
) -> None:
    core.answer = httpx.Response(200, json={"workspaces": [row(), row(OTHER, title="Trip")]})
    manager(
        monkeypatch,
        in_progress=[
            assignment(status="waiting", open_question="Which pantry?"),
            assignment(
                id=32,
                status="waiting",
                workspace=OTHER,
                open_request={"kind": "repo", "ref": "pantry", "reason": "the code"},
            ),
        ],
    )
    marks = charms(client, core)
    assert marks[WS] == {"state": "waiting", "reason": "assignment 31 has a question"}
    assert marks[OTHER] == {"state": "waiting", "reason": "assignment 32 requests a repository"}

    # A draft waits for a start, and so does its workspace.
    manager(monkeypatch, draft=[assignment(id=33, status="draft", state="draft")])
    assert charms(client, core)[WS] == {
        "state": "waiting",
        "reason": "assignment 33 waits for a start",
    }


def test_a_workspace_with_nothing_in_flight_and_nothing_waiting_carries_no_charm(
    client, core, monkeypatch
) -> None:
    core.answer = httpx.Response(200, json={"workspaces": [row()]})
    # An assignment of another person's workspace, one that ended, and one
    # that waits for the limits to reset: none of them is a charm here.
    manager(
        monkeypatch,
        in_progress=[
            assignment(workspace=OTHER),
            assignment(id=34, status="paused_limits"),
            assignment(id=35, status="running", workspace=None),
        ],
    )
    assert charms(client, core) == {WS: None}


def test_working_wins_over_waiting_on_one_workspace(client, core, monkeypatch) -> None:
    core.answer = httpx.Response(200, json={"workspaces": [row()]})
    manager(
        monkeypatch,
        draft=[assignment(id=36, status="draft", state="draft")],
        in_progress=[assignment()],
    )
    assert charms(client, core)[WS] == {"state": "working", "reason": "assignment 31 is running"}


def test_the_read_of_one_workspace_carries_the_charm_too(client, core, monkeypatch) -> None:
    core.answer = httpx.Response(200, json=row())
    manager(monkeypatch, in_progress=[assignment(status="waiting")])
    answer = client.get(f"{BASE}/{WS}", auth=ALEX)
    assert answer.json()["charm"] == {"state": "waiting", "reason": "assignment 31 has a question"}


def test_a_manager_that_is_absent_or_silent_leaves_the_other_sources(
    client, core, monkeypatch
) -> None:
    """A source that is not configured, or that does not answer, takes nothing
    away: the charm is what the rest of the sources say."""
    core.answer = httpx.Response(200, json={"workspaces": [row(turn_in_flight=True)]})
    monkeypatch.setattr(am, "from_env", lambda env: None)
    assert charms(client, core)[WS]["state"] == "working"
    manager(monkeypatch, down=True)
    assert charms(client, core)[WS]["state"] == "working"
    core.answer = httpx.Response(200, json={"workspaces": [row()]})
    assert charms(client, core) == {WS: None}
