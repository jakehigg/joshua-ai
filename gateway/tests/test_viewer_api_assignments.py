"""Offline tests for the JSON routes of the assignments.

``GET /api/v1/assignments`` lists the assignments of the person who signed
in, one state at a time. ``GET /api/v1/assignments/{id}`` gives one
assignment: its numbers, its definition as properties and blocks, its plan
with the progress of its task boxes, and the comments on each text. The two
comment routes write one comment and remove one.

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
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
from joshua_gateway.viewer_api import assignments as api
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
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

PLAN = "# Plan\n\n- [x] read the pages\n- [ ] fix the links\n"


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 7,
        "title": "Clean up the docs",
        "status": "running",
        "state": "in_progress",
        "model": "sonnet",
        "created_at": _iso(timedelta(hours=3)),
        "started_at": _iso(timedelta(hours=2)),
        "finished_at": None,
        "working_s": 4800,
        "turns": 14,
        "usage": {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "total": 12345},
        "open_question": None,
        "asked_at": None,
        "stopped_at_limits": None,
        "saved": None,
    }
    return base | fields


def detail(**fields: Any) -> dict[str, Any]:
    base = item() | {
        "spec": {"goal": "Clean up the docs.", "model": "sonnet", "budget": {"turns": 40}},
        "definition": DEFINITION,
        "plan": PLAN,
        "plan_at": _iso(timedelta(minutes=5)),
        "report": None,
        "report_at": None,
        "outputs": [],
        "checkpoints": [
            {"at": _iso(timedelta(minutes=5)), "note": "fixed two links"},
            {"at": _iso(timedelta(minutes=50)), "note": "read the pages"},
        ],
        "comments": [],
        "error": None,
    }
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
        key = request.url.path
        state = request.url.params.get("state")
        if state is not None:
            key = f"{key}?state={state}"
        answer = self.routes.get(key, 404)
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
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
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


def _list(**params: Any) -> httpx.Response:
    return client().get("/api/v1/assignments", params=params, auth=ALEX)


def _one(assignment_id: int = 7) -> httpx.Response:
    return client().get(f"/api/v1/assignments/{assignment_id}", auth=ALEX)


def _token(assignment_id: int = 7, person: str = "alex") -> str:
    return viewer._csrf_token(person, api.comments_scope(assignment_id))


def _add(assignment_id: int = 7, **fields: Any) -> httpx.Response:
    body = {"csrf": _token(assignment_id), **fields}
    return client().post(f"/api/v1/assignments/{assignment_id}/comments", json=body, auth=ALEX)


# -- the list ---------------------------------------------------------------


def test_the_list_takes_those_in_progress_first(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [item()]}})
    )
    response = _list()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["configured"] is True and body["available"] is True
    assert body["state"] == "in_progress"
    assert body["states"] == [{"key": key, "label": label} for key, label in am.STATES]
    assert body["timezone"] == "UTC" and body["now"] == NOW.isoformat()
    (row,) = body["items"]
    assert row == {
        "id": 7,
        "url": "/assignments/7",
        "title": "Clean up the docs",
        "state": "in_progress",
        "status": "running",
        "status_label": "running",
        "when": "started 2h ago",
        "duration": "1h 20m",
        "cost": "12k tokens",
        "turns": 14,
        "summary_line": "",
        "asks": False,
        "start_phrase": "",
    }
    # One read of the manager gives the whole page: no read of a detail.
    assert [r.url.path for r in manager.requests] == ["/v1/assignments"]


def test_the_list_takes_the_drafts_when_nothing_is_in_progress(data, monkeypatch) -> None:
    drafted = item(id=9, title="Review first", status="draft", state="draft", started_at=None)
    manager = _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=draft": {"items": [drafted]},
            }
        ),
    )
    body = _list().json()
    assert body["state"] == "draft"
    (row,) = body["items"]
    assert row["title"] == "Review first" and row["cost"] == ""
    assert row["when"] == "created 3h ago"
    assert row["start_phrase"] == "Tell Joshua: start assignment 9"
    assert [r.url.params.get("state") for r in manager.requests] == ["in_progress", "draft"]


def test_the_list_takes_the_completed_ones_last(data, monkeypatch) -> None:
    done = item(
        id=5,
        title="Old job",
        status="done",
        state="completed",
        finished_at=_iso(timedelta(minutes=30)),
        lede="Your wiki has no dead links now.",
    )
    manager = _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=draft": {"items": []},
                "/v1/assignments?state=completed": {"items": [done]},
            }
        ),
    )
    body = _list().json()
    assert body["state"] == "completed"
    (row,) = body["items"]
    assert row["when"] == "ended 30m ago"
    assert row["summary_line"] == "Your wiki has no dead links now."
    assert [r.url.params.get("state") for r in manager.requests] == [
        "in_progress",
        "draft",
        "completed",
    ]


def test_an_empty_state_still_names_the_state_and_what_it_says(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=draft": {"items": []},
                "/v1/assignments?state=completed": {"items": []},
            }
        ),
    )
    body = _list().json()
    assert body["state"] == "completed" and body["items"] == []
    assert body["empty"] == "No assignment is completed."


@pytest.mark.parametrize(
    ("state", "empty"),
    [
        ("draft", "No draft."),
        ("in_progress", "No assignment is in progress."),
        ("completed", "No assignment is completed."),
    ],
)
def test_a_state_of_the_query_is_the_only_one_read(data, monkeypatch, state, empty) -> None:
    manager = _with(monkeypatch, FakeManager({f"/v1/assignments?state={state}": {"items": []}}))
    body = _list(state=state).json()
    assert body["state"] == state and body["empty"] == empty
    (request,) = manager.requests
    assert request.headers["Authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert request.url.params["person"] == "alex"
    assert request.url.params["state"] == state
    assert request.url.params["limit"] == str(am.LIST_LIMIT)


def test_a_state_that_is_not_a_state_gives_the_default(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [item()]}})
    )
    assert _list(state="nope").json()["state"] == "in_progress"
    assert manager.requests[0].url.params["state"] == "in_progress"


def test_a_question_marks_the_row(data, monkeypatch) -> None:
    asking = item(status="waiting", open_question="Which repo?", next_step="fix the links")
    _with(monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [asking]}}))
    (row,) = _list().json()["items"]
    assert row["asks"] is True
    assert row["status_label"] == "waiting"
    assert row["summary_line"] == "Next: fix the links"


def test_the_list_without_the_manager_says_it_is_not_configured(data) -> None:
    response = _list()
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False and body["available"] is False
    assert body["state"] is None and body["items"] == []


def test_the_list_when_the_manager_does_not_answer(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager(status=503))
    body = _list().json()
    assert body["configured"] is True and body["available"] is False
    assert body["state"] is None and body["items"] == []


def test_no_state_is_picked_when_the_second_read_fails(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=draft": 503,
            }
        ),
    )
    body = _list().json()
    assert body["available"] is False and body["state"] is None


def test_an_explicit_state_that_fails_gives_no_state(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments?state=draft": 503}))
    body = _list(state="draft").json()
    assert body["available"] is False and body["state"] is None


# -- one assignment ---------------------------------------------------------


def test_one_assignment_has_its_definition_its_plan_and_no_report(data, monkeypatch) -> None:
    body = detail(
        saved={"id": 3, "name": "docs-cleanup"},
        outputs=[{"kind": "task", "ref": "tasks#12", "title": "Follow up", "at": None}],
        comments=[comment(), comment(id=12, target="plan", line_no=3, body="Why?")],
        open_comments=2,
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    response = _one()
    assert response.status_code == 200
    answer = response.json()
    assert answer["configured"] is True and answer["available"] is True
    assert answer["not_found"] is False
    assert answer["refresh_seconds"] == am.REFRESH_S
    assert answer["timezone"] == "UTC" and answer["now"] == NOW.isoformat()
    assert isinstance(answer["csrf"], str) and answer["csrf"] != ""

    one = answer["item"]
    assert one["title"] == "Clean up the docs" and one["state"] == "in_progress"
    assert one["status_label"] == "running" and one["when"] == "started 2h ago"
    assert one["duration"] == "1h 20m" and one["cost"] == "12k tokens"
    assert one["turns"] == 14 and one["usage"]["total"] == 12345
    assert one["saved_ref"] == {"id": 3, "name": "docs-cleanup"}
    assert one["outputs"] == [{"kind": "task", "ref": "tasks#12", "title": "Follow up", "when": ""}]
    assert [point["note"] for point in one["checkpoints"]] == ["fixed two links", "read the pages"]
    assert one["checkpoints"][0]["when"] == "5m ago"
    assert one["open_request"] is None and one["notice"] is None
    assert one["lineage"] == [] and one["run_number"] is None

    definition = answer["definition"]
    assert definition["lines"] == DEFINITION.split("\n")
    assert definition["goal_offset"] == 2
    units = definition["units"]
    assert [(u["id"], u["kind"]) for u in units] == [
        ("L1", "prop"),
        ("L4", "prop"),
        ("L2", "goal"),
        ("L3", "p"),
    ]
    assert units[0]["label"] == "title"
    assert units[0]["value"] == {"kind": "text", "text": "Clean up the docs"}
    assert units[3]["markdown"] == "Clean up the docs."
    assert units[3]["what"] == "the paragraph at line 3"

    plan = answer["plan"]
    assert plan["markdown"] == PLAN
    assert plan["lines"] == PLAN.split("\n")
    assert plan["progress"] == {"done": 1, "total": 2, "next": "fix the links", "percent": 50.0}
    assert [block["id"] for block in plan["blocks"]] == ["P1", "P3", "P4"]
    assert plan["updated"] == "5m ago"

    assert answer["report"] is None
    assert [c["id"] for c in answer["comments"]["definition"]] == ["11"]
    assert answer["comments"]["definition"][0]["author"] == "alex"
    assert [c["id"] for c in answer["comments"]["plan"]] == ["12"]
    assert answer["comments"]["report"] == []


def test_a_report_comes_as_markdown_blocks_and_sections(data, monkeypatch) -> None:
    report = "# Clean up the docs\n\nThe lede.\n\n## What was done\n\nFixed two links.\n"
    body = detail(
        status="done",
        state="completed",
        finished_at=_iso(timedelta(minutes=2)),
        report=report,
        report_at=_iso(timedelta(minutes=2)),
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    answer = _one().json()
    assert answer["refresh_seconds"] is None
    part = answer["report"]
    assert part["markdown"] == report
    assert part["title"] == "Clean up the docs" and part["lede"] == "The lede."
    assert part["structured"] is True
    assert part["sections"]["what was done"]["title"] == "What was done"
    assert [block["id"] for block in part["blocks"]] == ["R1", "R3", "R5", "R7"]


def test_a_draft_says_how_to_start_it_and_has_no_cost(data, monkeypatch) -> None:
    body = detail(
        id=9,
        title="Review the docs first",
        status="draft",
        state="draft",
        started_at=None,
        working_s=0,
        turns=None,
        plan=None,
        plan_at=None,
        checkpoints=[],
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/9": body}))
    answer = _one(9).json()
    assert answer["refresh_seconds"] is None
    one = answer["item"]
    assert one["start_phrase"] == "Tell Joshua: start assignment 9"
    assert one["draft_line"] == am.DRAFT_LINE
    assert one["cost"] == "" and one["turns"] is None
    assert answer["plan"]["markdown"] == "" and answer["plan"]["blocks"] == []
    assert answer["plan"]["progress"] == {"done": 0, "total": 0, "next": "", "percent": 0.0}


def test_a_manager_with_no_definition_text_gives_the_spec_and_no_token(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail(definition=None)}))
    answer = _one().json()
    assert answer["csrf"] is None
    assert answer["definition"]["units"] == []
    assert "goal: Clean up the docs." in "\n".join(answer["definition"]["lines"])


def test_a_notice_says_what_the_assignment_waits_for(data, monkeypatch) -> None:
    body = detail(
        status="waiting",
        open_question="Which branch?",
        open_request={"kind": "repo", "ref": "example/docs", "reason": "To read it."},
        asked_at=_iso(timedelta(minutes=5)),
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    one = _one().json()["item"]
    assert one["notice"] == {
        "kind": "request",
        "heading": "The assignment requests a repository",
        "when": "5m ago",
        "ref": "example/docs",
        "text": "Reason: To read it.",
        "foot": "Joshua grants it, provides it, or refuses it.",
    }
    assert one["open_request"]["kind"] == "repo"


def test_a_question_and_a_limits_stop_are_notices(data, monkeypatch) -> None:
    asking = detail(status="waiting", open_question="Which branch?")
    _with(monkeypatch, FakeManager({"/v1/assignments/7": asking}))
    notice = _one().json()["item"]["notice"]
    assert notice["kind"] == "question" and notice["text"] == "Which branch?"

    stop = {"window": "five_hour", "used_pct": 91.5, "ceiling": 90, "next_step": "fix the links"}
    _with(
        monkeypatch,
        FakeManager({"/v1/assignments/7": detail(status="paused_limits", stopped_at_limits=stop)}),
    )
    notice = _one().json()["item"]["notice"]
    assert notice["kind"] == "limits"
    assert notice["text"].startswith("Stopped at the 5-hour limit: 91.5% used")


def test_a_rerun_says_which_run_it_is(data, monkeypatch) -> None:
    runs = [
        {"id": 4, "title": "Clean up the docs", "status": "done", "state": "completed"},
        {"id": 7, "title": "Clean up the docs", "status": "running", "state": "in_progress"},
    ]
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail(parent_id=4, lineage=runs)}))
    one = _one().json()["item"]
    assert one["run_number"] == {"run": 2, "root": 4}
    assert [run["id"] for run in one["lineage"]] == [4, 7]
    assert [run["current"] for run in one["lineage"]] == [False, True]
    assert one["lineage"][0]["url"] == "/assignments/4"


def test_an_assignment_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}))
    response = _one(99)
    assert response.status_code == 404
    body = response.json()
    assert body["not_found"] is True and body["item"] is None
    assert body["configured"] is True


def test_one_assignment_without_the_manager_says_it_is_not_configured(data) -> None:
    response = _one()
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False and body["available"] is False
    assert body["not_found"] is False and body["item"] is None
    assert body["csrf"] is None and body["refresh_seconds"] is None


def test_one_assignment_when_the_manager_does_not_answer(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager(status=503))
    response = _one()
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True and body["available"] is False
    assert body["item"] is None


def test_a_path_that_is_not_a_number_is_404(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    assert client().get("/api/v1/assignments/abc", auth=ALEX).status_code == 404
    assert manager.requests == []


# -- the comments -----------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "line_no", "anchor"),
    [("definition", 4, "L4"), ("plan", 3, "P3"), ("report", 6, "R6")],
)
def test_a_comment_on_each_text_goes_to_the_manager(
    data, monkeypatch, target, line_no, anchor
) -> None:
    manager = _with(monkeypatch, FakeManager({"/v1/assignments/7": detail()}))
    response = _add(target=target, line_no=line_no, line_text="model: sonnet", body="Use opus.")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "anchor": anchor}
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert sent.url.path == "/v1/assignments/7/comments"
    assert sent.url.params["person"] == "alex"
    assert sent.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    fields = {"line_no": line_no, "line_text": "model: sonnet", "body": "Use opus."}
    if target != "definition":
        fields["target"] = target
    assert json.loads(sent.content) == fields


def test_a_comment_with_no_target_is_on_the_definition(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = _add(line_no=1, line_text="title: x", body="Rename it.")
    assert response.json() == {"ok": True, "anchor": "L1"}
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert json.loads(sent.content)["line_no"] == 1


def test_a_line_that_is_not_a_number_is_line_zero(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = _add(line_no="x", line_text="", body="The whole thing.")
    assert response.json() == {"ok": True, "anchor": "L0"}
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert json.loads(sent.content)["line_no"] == 0


def test_a_bad_target_is_refused_and_not_sent(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = _add(target="summary", line_no=1, line_text="x", body="y")
    assert response.status_code == 400
    assert response.json()["error"] == api.BAD_TARGET
    assert manager.requests == []


def test_a_comment_needs_the_token_of_its_own_assignment(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    fields = {"target": "plan", "line_no": 1, "line_text": "x", "body": "y"}
    for token in ("", "nope", _token(8)):
        response = client().post(
            "/api/v1/assignments/7/comments", json={"csrf": token, **fields}, auth=ALEX
        )
        assert response.status_code == 403
    assert manager.requests == []


def test_a_comment_from_another_site_is_refused(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = client().post(
        "/api/v1/assignments/7/comments",
        json={"csrf": _token(), "line_no": 1, "line_text": "x", "body": "y"},
        headers={"Origin": "https://elsewhere.example"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert manager.requests == []


def test_a_body_that_is_not_json_is_refused(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    response = client().post(
        "/api/v1/assignments/7/comments",
        content="not json",
        headers={"content-type": "application/json"},
        auth=ALEX,
    )
    assert response.status_code == 400
    assert manager.requests == []


def test_a_comment_write_needs_a_sign_in(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    for path in ("/api/v1/assignments/7/comments", "/api/v1/assignments/7/comments/1/delete"):
        assert client().post(path, json={"csrf": "x"}).status_code == 401
    assert manager.requests == []


def test_a_comment_goes_nowhere_without_the_manager(data) -> None:
    response = _add(line_no=1, line_text="x", body="y")
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("write", "error", "status", "text"),
    [
        (404, "not found", 404, "not found"),
        (400, "the line is not in the text", 400, "the line is not in the text"),
        (409, "a resolved comment stays", 400, "a resolved comment stays"),
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
        "/api/v1/assignments/7/comments/11/delete",
        json={"csrf": _token(), "target": "plan"},
        auth=ALEX,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "anchor": "plan"}
    [gone] = [r for r in manager.requests if r.method == "DELETE"]
    assert gone.url.path == "/v1/assignments/7/comments/11"
    assert gone.url.params["person"] == "alex"


def test_a_delete_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}, write=404, error="not found"))
    response = client().post(
        "/api/v1/assignments/7/comments/11/delete", json={"csrf": _token()}, auth=ALEX
    )
    assert response.status_code == 404


def test_a_delete_needs_the_token_and_the_manager(data, monkeypatch) -> None:
    assert (
        client()
        .post("/api/v1/assignments/7/comments/11/delete", json={"csrf": _token()}, auth=ALEX)
        .status_code
        == 404
    )
    manager = _with(monkeypatch, FakeManager({}, write=204))
    response = client().post(
        "/api/v1/assignments/7/comments/11/delete", json={"csrf": "nope"}, auth=ALEX
    )
    assert response.status_code == 403
    assert manager.requests == []
