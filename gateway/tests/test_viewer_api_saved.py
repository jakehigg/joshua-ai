"""Offline tests for the JSON routes of the saved assignments.

``GET /api/v1/assignments/saved`` lists the saved assignments of the person
who signed in, and ``GET /api/v1/assignments/saved/{id}`` gives one: its
definition as properties and blocks, its runs, and the comments on its
definition. A saved assignment takes a comment on its definition only, which
``targets`` says, and the two comment routes write one comment and remove
one.

A MockTransport stands in for the assignments manager, so no network is
needed, and the clock is fixed. With ``ASSIGNMENTS_URL`` or
``JOSHUA_TOKEN_VIEWER`` missing, every read answers 200 and says that the
manager is not configured. A row of another person is 404.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import signed_in
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
from joshua_gateway.viewer_api import assignments as api
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = signed_in("alex")
VIEWER_TOKEN = "viewer-token"
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: UTC
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)

DEFINITION = "title: Clean up the docs\ngoal: |-\n  Clean up the docs.\nmodel: sonnet"


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def run(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "title": "Clean up the docs",
        "status": "done",
        "state": "completed",
        "model": "sonnet",
        "created_at": _iso(timedelta(hours=3)),
        "started_at": _iso(timedelta(hours=2)),
        "finished_at": _iso(timedelta(hours=1)),
        "working_s": 4800,
        "turns": 14,
        "usage": {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "total": 12345},
    }
    return base | fields


def saved_item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 3,
        "name": "docs-cleanup",
        "title": "Clean up the docs",
        "spec": {"goal": "Clean up the docs.", "schedule": "every Monday at 9am"},
        "created_at": _iso(timedelta(days=10)),
        "updated_at": _iso(timedelta(days=2)),
        "runs": 2,
        "last_run": {"id": 7, "status": "done", "finished_at": _iso(timedelta(days=1))},
        "open_comments": 0,
    }
    return base | fields


def saved_detail(**fields: Any) -> dict[str, Any]:
    base = saved_item() | {"runs_list": [run()], "definition": DEFINITION, "comments": []}
    return base | fields


def comment(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 11,
        "target": "definition",
        "line_no": 4,
        "line_text": "model: sonnet",
        "body": "Use opus.",
        "created_at": _iso(timedelta(minutes=10)),
        "resolved_at": None,
        "resolution": None,
        "stale": False,
    }
    return base | fields


class FakeManager:
    """Answers by path. ``routes`` maps a path to a body, or to a status.
    ``write`` is the status of a comment write."""

    def __init__(
        self,
        routes: dict[str, Any] | None = None,
        status: int = 200,
        write: int = 201,
        error: str = "",
    ) -> None:
        self.routes = routes or {}
        self.status = status
        self.write = write
        self.error = error
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method in ("POST", "DELETE"):
            if self.write in (201, 204):
                return httpx.Response(self.write, json={} if self.write == 201 else None)
            return httpx.Response(self.write, json={"error": self.error})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        answer = self.routes.get(request.url.path, 404)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "no"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def data(monkeypatch, tmp_path) -> None:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.delenv(am.ASSIGNMENTS_URL_ENV, raising=False)
    monkeypatch.delenv(am.VIEWER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    monkeypatch.setattr(viewer, "_now", lambda tz: NOW.astimezone(tz))


def _with(monkeypatch, manager: FakeManager) -> FakeManager:
    transport = httpx.MockTransport(manager.handler)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", VIEWER_TOKEN, transport=transport),
    )
    return manager


def client() -> TestClient:
    return TestClient(viewer.build_app())


def _list() -> httpx.Response:
    return client().get("/api/v1/assignments/saved", auth=ALEX)


def _one(saved_id: int = 3) -> httpx.Response:
    return client().get(f"/api/v1/assignments/saved/{saved_id}", auth=ALEX)


def _token(saved_id: int = 3, person: str = "alex") -> str:
    return viewer._csrf_token(person, api.saved_scope(saved_id))


def _add(saved_id: int = 3, **fields: Any) -> httpx.Response:
    body = {"csrf": _token(saved_id), **fields}
    return client().post(f"/api/v1/assignments/saved/{saved_id}/comments", json=body, auth=ALEX)


# -- the list ---------------------------------------------------------------


def test_the_list_has_one_row_a_saved_assignment(data, monkeypatch) -> None:
    never = saved_item(id=4, name="weekly-audit", title="", spec={}, runs=0, last_run=None)
    manager = _with(monkeypatch, FakeManager({"/v1/saved": {"items": [saved_item(), never]}}))
    response = _list()
    assert response.status_code == 200
    answer = response.json()
    assert answer["configured"] is True and answer["available"] is True
    assert answer["timezone"] == "UTC" and answer["now"] == NOW.isoformat()
    assert answer["empty"] == api.SAVED_EMPTY
    first, second = answer["items"]
    assert first["id"] == 3 and first["url"] == "/assignments/saved/3"
    assert first["name"] == "docs-cleanup" and first["title"] == "Clean up the docs"
    assert first["schedule"] == "every Monday at 9am"
    assert first["run_count"] == 2
    assert first["last_run"] == {
        "id": 7,
        "url": "/assignments/7",
        "status": "done",
        "status_label": "done",
        "when": "24h ago",
    }
    # A saved assignment that only runs by name has no schedule and no run.
    assert second["schedule"] == "" and second["last_run"] is None
    assert second["run_count"] == 0
    [sent] = manager.requests
    assert sent.url.path == "/v1/saved" and sent.url.params["person"] == "alex"
    assert sent.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"


def test_an_empty_list_still_says_what_to_do(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": []}}))
    answer = _list().json()
    assert answer["items"] == [] and answer["available"] is True
    assert "Ask Joshua" in answer["empty"]


def test_the_list_without_the_manager_says_it_is_not_configured(data) -> None:
    answer = _list().json()
    assert answer["configured"] is False and answer["available"] is False
    assert answer["items"] == []


def test_the_list_when_the_manager_does_not_answer(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager(status=503))
    answer = _list().json()
    assert answer["configured"] is True and answer["available"] is False
    assert answer["items"] == []


def test_an_answer_of_an_unknown_shape_is_no_answer(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": "nope"}}))
    assert _list().json()["available"] is False


def test_each_read_needs_a_sign_in(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    for path in ("/api/v1/assignments/saved", "/api/v1/assignments/saved/3"):
        assert client().get(path).status_code == 401
    assert manager.requests == []


# -- one saved assignment ---------------------------------------------------


def test_one_saved_assignment_has_its_definition_its_runs_and_its_comments(
    data, monkeypatch
) -> None:
    body = saved_detail(
        runs=5,
        runs_list=[run(id=9, status="running", state="in_progress", finished_at=None), run()],
        comments=[comment(), comment(id=12, target="plan", body="Not here.")],
        open_comments=1,
    )
    manager = _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    response = _one()
    assert response.status_code == 200
    answer = response.json()
    assert answer["configured"] is True and answer["available"] is True
    assert answer["not_found"] is False
    assert answer["targets"] == ["definition"]
    assert isinstance(answer["csrf"], str) and answer["csrf"] != ""
    assert answer["timezone"] == "UTC" and answer["now"] == NOW.isoformat()

    one = answer["item"]
    assert one["name"] == "docs-cleanup" and one["run_count"] == 5
    assert one["updated"] == "2d ago" and one["open_comments"] == 1

    definition = answer["definition"]
    assert definition["lines"] == DEFINITION.split("\n")
    assert [(u["id"], u["kind"]) for u in definition["units"]] == [
        ("L1", "prop"),
        ("L4", "prop"),
        ("L2", "goal"),
        ("L3", "p"),
    ]

    # The newest run is run 5 of the saved assignment, and each run links to
    # its own page.
    assert [(r["id"], r["number"]) for r in answer["runs"]] == [(9, 5), (7, 4)]
    assert answer["runs"][0]["url"] == "/assignments/9"
    assert answer["runs"][0]["status_label"] == "running"
    assert answer["runs"][1]["when"] == "ended 1h ago"
    assert answer["runs"][1]["cost"] == "12k tokens"

    # Only a comment on the definition belongs to a saved assignment.
    assert [c["id"] for c in answer["comments"]] == ["11"]
    assert answer["comments"][0]["author"] == "alex"
    assert answer["comments"][0]["line_no"] == 4

    [sent] = manager.requests
    assert sent.url.path == "/v1/saved/3" and sent.url.params["person"] == "alex"


def test_a_saved_assignment_with_no_run_says_nothing_more(data, monkeypatch) -> None:
    body = saved_detail(runs=0, last_run=None, runs_list=[])
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    answer = _one().json()
    assert answer["runs"] == [] and answer["item"]["last_run"] is None


def test_a_manager_with_no_definition_text_gives_the_spec_and_no_token(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/saved/3": saved_detail(definition=None)}))
    answer = _one().json()
    assert answer["csrf"] is None
    assert answer["definition"]["units"] == []
    assert "goal: Clean up the docs." in "\n".join(answer["definition"]["lines"])


def test_a_saved_assignment_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}))
    response = _one(99)
    assert response.status_code == 404
    answer = response.json()
    assert answer["not_found"] is True and answer["item"] is None


def test_one_saved_assignment_without_the_manager_says_it_is_not_configured(data) -> None:
    response = _one()
    assert response.status_code == 200
    answer = response.json()
    assert answer["configured"] is False and answer["available"] is False
    assert answer["item"] is None and answer["targets"] == ["definition"]


def test_one_saved_assignment_when_the_manager_does_not_answer(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager(status=503))
    response = _one()
    assert response.status_code == 200
    answer = response.json()
    assert answer["configured"] is True and answer["available"] is False
    assert answer["item"] is None and answer["runs"] == []


def test_a_path_that_is_not_a_number_is_404(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    assert client().get("/api/v1/assignments/saved/abc", auth=ALEX).status_code == 404
    assert manager.requests == []


# -- the comments -----------------------------------------------------------


def test_a_comment_on_the_definition_goes_to_the_manager(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({"/v1/saved/3": saved_detail()}))
    response = _add(line_no=4, line_text="model: sonnet", body="Use opus.")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "anchor": "L4"}
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert sent.url.path == "/v1/saved/3/comments"
    assert sent.url.params["person"] == "alex"
    assert sent.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert json.loads(sent.content) == {
        "line_no": 4,
        "line_text": "model: sonnet",
        "body": "Use opus.",
    }


def test_a_target_that_is_not_the_definition_is_refused_and_not_sent(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    for target in ("plan", "report", "summary"):
        response = _add(target=target, line_no=1, line_text="x", body="y")
        assert response.status_code == 400, target
        assert response.json()["error"] == api.BAD_TARGET
    assert manager.requests == []


def test_a_comment_needs_the_token_of_its_own_saved_assignment(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    fields = {"line_no": 1, "line_text": "x", "body": "y"}
    tokens = ("", "nope", _token(4), viewer._csrf_token("alex", api.comments_scope(3)))
    for token in tokens:
        response = client().post(
            "/api/v1/assignments/saved/3/comments", json={"csrf": token, **fields}, auth=ALEX
        )
        assert response.status_code == 403
    assert manager.requests == []


def test_a_comment_from_another_site_is_refused(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = client().post(
        "/api/v1/assignments/saved/3/comments",
        json={"csrf": _token(), "line_no": 1, "line_text": "x", "body": "y"},
        headers={"Origin": "https://elsewhere.example"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert manager.requests == []


def test_a_body_that_is_not_json_is_refused(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = client().post(
        "/api/v1/assignments/saved/3/comments",
        content="not json",
        headers={"content-type": "application/json"},
        auth=ALEX,
    )
    assert response.status_code == 400
    assert manager.requests == []


def test_a_comment_write_needs_a_sign_in(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    paths = (
        "/api/v1/assignments/saved/3/comments",
        "/api/v1/assignments/saved/3/comments/1/delete",
    )
    for path in paths:
        assert client().post(path, json={"csrf": "x"}).status_code == 401
    assert manager.requests == []


def test_a_comment_goes_nowhere_without_the_manager(data) -> None:
    assert _add(line_no=1, line_text="x", body="y").status_code == 404


@pytest.mark.parametrize(
    ("write", "error", "status", "text"),
    [
        (404, "not found", 404, "not found"),
        (400, "the line is not in the text", 400, "the line is not in the text"),
        (503, "", 400, "The manager refused the comment."),
    ],
)
def test_a_refused_comment_says_why(data, monkeypatch, write, error, status, text) -> None:
    _with(monkeypatch, FakeManager({}, write=write, error=error))
    response = _add(line_no=1, line_text="x", body="y")
    assert response.status_code == status
    assert response.json()["error"] == text


def test_a_comment_when_the_manager_does_not_answer(data, monkeypatch) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", VIEWER_TOKEN, transport=transport),
    )
    monkeypatch.setattr("joshua_shared.http.BACKOFF_BASE_S", 0.0)
    response = _add(line_no=1, line_text="x", body="y")
    assert response.status_code == 400
    assert response.json()["error"] == "the manager did not answer"


def test_a_delete_goes_to_the_manager(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}, write=204))
    response = client().post(
        "/api/v1/assignments/saved/3/comments/11/delete", json={"csrf": _token()}, auth=ALEX
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "anchor": "definition"}
    [gone] = [r for r in manager.requests if r.method == "DELETE"]
    assert gone.url.path == "/v1/saved/3/comments/11"
    assert gone.url.params["person"] == "alex"


def test_a_delete_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}, write=404, error="not found"))
    response = client().post(
        "/api/v1/assignments/saved/3/comments/11/delete", json={"csrf": _token()}, auth=ALEX
    )
    assert response.status_code == 404


def test_a_delete_needs_the_token_and_the_manager(data, monkeypatch) -> None:
    path = "/api/v1/assignments/saved/3/comments/11/delete"
    assert client().post(path, json={"csrf": _token()}, auth=ALEX).status_code == 404
    manager = _with(monkeypatch, FakeManager({}, write=204))
    assert client().post(path, json={"csrf": "nope"}, auth=ALEX).status_code == 403
    assert manager.requests == []
