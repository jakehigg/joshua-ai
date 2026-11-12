"""Offline tests for the assignments manager client and the saved pages.

A MockTransport stands in for the assignments manager, so no network is
needed. The answers follow the read API contract of the manager.

``/assignments`` and ``/assignments/<id>`` are the React app, and
``tests/test_viewer_api_assignments.py`` covers their JSON routes.
``/assignments/saved`` and ``/assignments/saved/<id>`` are still rendered
here, with the definition as a document and a comment form on each line.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
from joshua_gateway import viewer_comments as vc
from joshua_gateway import viewer_showcase as sc
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
        "usage": {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "total": 12345},
        "open_question": None,
        "asked_at": None,
        "stopped_at_limits": None,
        "saved": None,
    }
    return base | fields


def saved_item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 3,
        "name": "docs-cleanup",
        "title": "Clean up the docs",
        "spec": {"goal": "Clean up the docs."},
        "created_at": _iso(timedelta(days=10)),
        "updated_at": _iso(timedelta(days=2)),
        "runs": 2,
        "last_run": {"id": 7, "status": "done", "finished_at": _iso(timedelta(days=1))},
    }
    return base | fields


class FakeManager:
    """Answers by path. ``routes`` maps a path to a body, or to a status."""

    def __init__(self, routes: dict[str, Any] | None = None, status: int = 200) -> None:
        self.routes = routes or {}
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
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
def data(monkeypatch, tmp_path) -> Path:
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
    return root


def _with(monkeypatch, manager: FakeManager) -> FakeManager:
    transport = httpx.MockTransport(manager.handler)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", VIEWER_TOKEN, transport=transport),
    )
    return manager


def _get(path: str, **params: Any) -> httpx.Response:
    return TestClient(viewer.build_app()).get(path, params=params, auth=ALEX)


def _main(html: str) -> str:
    return html[html.index("<main") : html.index("</main>")]


# -- the nav and the sign-in -------------------------------------------------


def test_the_nav_has_the_assignments_section(data) -> None:
    html = _get("/assignments/saved").text
    nav = html[html.index('<nav class="v-sections"') :]
    nav = nav[: nav.index("</nav>")]
    assert nav.index('href="/usage"') < nav.index('href="/assignments"')
    assert '<svg class="j-icon"' in nav[nav.index('href="/assignments"') :]
    assert 'class="j-tab" href="/assignments"' in html


def test_the_current_section_is_marked(data) -> None:
    html = _get("/assignments/saved").text
    assert 'class="j-nav-item" href="/assignments" aria-current="page"' in html
    assert 'class="j-tab" href="/assignments" aria-current="page"' in html
    assert "<strong>Assignments</strong>" in html


@pytest.mark.parametrize("path", ["/assignments/saved", "/assignments/saved/3"])
def test_each_saved_page_needs_a_sign_in(data, path) -> None:
    assert TestClient(viewer.build_app()).get(path).status_code == 401


# -- not configured, and a manager that does not answer -----------------------


@pytest.mark.parametrize("path", ["/assignments/saved", "/assignments/saved/3"])
def test_without_the_manager_each_saved_page_says_it_is_not_configured(data, path) -> None:
    response = _get(path)
    assert response.status_code == 200
    assert "not configured" in response.text
    assert am.ASSIGNMENTS_URL_ENV in response.text


@pytest.mark.parametrize("path", ["/assignments/saved", "/assignments/saved/3"])
def test_a_manager_that_fails_gives_a_page_with_nothing_old(data, monkeypatch, path) -> None:
    _with(monkeypatch, FakeManager(status=503))
    response = _get(path)
    assert response.status_code == 200
    assert "did not answer" in response.text
    assert "j-list-row" not in response.text


def test_an_unknown_shape_is_a_failure(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": "nope"}}))
    assert "did not answer" in _get("/assignments/saved").text


async def test_a_network_error_is_none() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = am.AssignmentsClient("http://assignments", "t", transport=httpx.MockTransport(fail))
    assert await client.list("alex", "draft") is None
    assert await client.detail("alex", 1) is None


async def test_a_body_that_is_not_json_is_none() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html>"))
    client = am.AssignmentsClient("http://assignments", "t", transport=transport)
    assert await client.saved("alex") is None


def test_from_env_needs_both_values() -> None:
    assert am.from_env({}) is None
    assert am.from_env({am.ASSIGNMENTS_URL_ENV: "http://assignments"}) is None
    assert am.from_env({am.VIEWER_TOKEN_ENV: "t"}) is None
    client = am.from_env({am.ASSIGNMENTS_URL_ENV: "http://assignments", am.VIEWER_TOKEN_ENV: "t"})
    assert isinstance(client, am.AssignmentsClient)


def test_readyz_reports_the_assignments(data, monkeypatch) -> None:
    client = TestClient(viewer.build_app())
    assert client.get("/readyz").json()["assignments"] is False
    monkeypatch.setenv(am.ASSIGNMENTS_URL_ENV, "http://assignments")
    monkeypatch.setenv(am.VIEWER_TOKEN_ENV, "t")
    assert client.get("/readyz").json()["assignments"] is True


# -- the saved pages ---------------------------------------------------------


def test_each_saved_route_passes_the_person_and_the_token(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch,
        FakeManager({"/v1/saved": {"items": []}, "/v1/saved/3": saved_item() | {"runs_list": []}}),
    )
    for path in ("/assignments/saved", "/assignments/saved/3"):
        assert _get(path).status_code == 200, path
    assert [r.url.params["person"] for r in manager.requests] == ["alex", "alex"]
    assert [r.url.path for r in manager.requests] == ["/v1/saved", "/v1/saved/3"]
    assert {r.headers["Authorization"] for r in manager.requests} == {f"Bearer {VIEWER_TOKEN}"}


def test_the_headings_are_lower_case_as_in_the_other_sections(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager({"/v1/saved": {"items": []}, "/v1/saved/3": saved_item() | {"runs_list": []}}),
    )
    saved = _get("/assignments/saved").text
    assert '<h1 class="j-doc-title">saved assignments</h1>' in saved
    assert "<title>saved assignments</title>" in saved
    one = _get("/assignments/saved/3").text
    assert '<a href="/assignments">assignments</a>' in one
    assert '<a href="/assignments/saved">saved</a>' in one


def test_the_saved_list_has_one_row_a_saved_assignment(data, monkeypatch) -> None:
    never = saved_item(id=4, name="weekly-audit", title="", runs=0, last_run=None)
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": [saved_item(), never]}}))
    html = _main(_get("/assignments/saved").text)
    assert "<table" not in html
    assert 'href="/assignments/saved/3">docs-cleanup</a>' in html
    assert "Clean up the docs" in html
    assert "2 runs" in html and "0 runs" in html
    assert 'href="/assignments/7">last run</a>' in html
    assert '<span class="j-badge ok">done</span>' in html
    assert 'href="/assignments/saved/4">weekly-audit</a>' in html


def test_an_empty_saved_list_says_so(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": []}}))
    assert "No saved assignment." in _get("/assignments/saved").text


def test_an_unknown_status_is_a_plain_chip(data, monkeypatch) -> None:
    odd = saved_item(last_run={"id": 7, "status": "odd_<x>", "finished_at": None})
    _with(monkeypatch, FakeManager({"/v1/saved": {"items": [odd]}}))
    assert '<span class="j-badge plain">odd &lt;x&gt;</span>' in _get("/assignments/saved").text


def test_the_saved_detail_has_the_definition_open_and_the_runs(data, monkeypatch) -> None:
    runs = [item(id=9, title="Second run"), item(id=7, title="First run", status="done")]
    body = saved_item(runs=1) | {"runs_list": runs}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    html = _main(_get("/assignments/saved/3").text)
    assert "<table" not in html
    assert '<h1 class="j-doc-title">docs-cleanup</h1>' in html
    assert "1 run ·" in html
    assert '<details class="a-def" open id="definition"><summary>Definition</summary>' in html
    assert html.index('href="/assignments/9"') < html.index('href="/assignments/7"')
    # A run row gives the status, when it started, the model and the tokens.
    assert '<span class="j-badge accent">running</span>' in html
    assert "started <time" in html and ">2h ago</time>" in html
    assert 'title="2026-11-09 13:00 UTC"' in html
    assert "sonnet" in html and "12k tokens" in html


def test_a_saved_detail_with_no_run_says_so(data, monkeypatch) -> None:
    body = saved_item(runs=0, last_run=None) | {"runs_list": []}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    assert "No run yet." in _get("/assignments/saved/3").text


def test_a_saved_assignment_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}))
    assert _get("/assignments/saved/99").status_code == 404


def test_a_hostile_title_and_model_are_escaped(data, monkeypatch) -> None:
    bad = item(title="<script>alert(1)</script>", model='"><img src=x>')
    body = saved_item() | {"runs_list": [bad]}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    html = _get("/assignments/saved/3").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html
    assert '"><img src=x>' not in html


# -- the small parts ---------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-30, "just now"),
        (59, "just now"),
        (60 * 5, "5m ago"),
        (3600 * 2, "2h ago"),
        (3600 * 47, "47h ago"),
        (86400 * 3, "3d ago"),
        (86400 * 90, "3mo ago"),
    ],
)
def test_ago(seconds, text) -> None:
    assert vc.ago(seconds) == text


def test_duration() -> None:
    assert am.duration(-1) == "0s"
    assert am.duration(45) == "45s"
    assert am.duration(12 * 60) == "12m"
    assert am.duration(80 * 60) == "1h 20m"


def test_time_html_needs_an_offset() -> None:
    assert vc.time_html(None, tz=UTC, now=NOW) == ""
    assert vc.time_html("2026-11-09T10:00:00", tz=UTC, now=NOW) == ""
    assert vc.time_html("not a time", tz=UTC, now=NOW) == ""


def test_is_url() -> None:
    assert sc.is_url("https://git.example.com/x")
    assert sc.is_url("http://example.com")
    assert not sc.is_url("javascript:alert(1)")
    assert not sc.is_url("https://")
    assert not sc.is_url("tasks#12")
    assert not sc.is_url("http://[bad")


def test_parse_state() -> None:
    assert am.parse_state("draft") == "draft"
    assert am.parse_state("queued") is None
    assert am.parse_state(None) is None
    assert am.parse_state("running") is None


def test_the_states_are_drafts_in_progress_and_completed() -> None:
    assert [label for _, label in am.STATES] == ["Drafts", "In progress", "Completed"]
    assert am.empty_text("draft") == "No draft."
    assert am.empty_text(None) == "No assignment here."


def test_a_stop_on_unknown_limits_says_so() -> None:
    text = am.limits_text({"reason": "unknown"}, tz=UTC, now=NOW)
    assert text == "Stopped because the subscription limits are not known."


def test_a_limits_stop_reads_the_window_and_the_reset(data) -> None:
    stop = {
        "window": "five_hour",
        "used_pct": 91.5,
        "ceiling": 90,
        "resets_at": (NOW + timedelta(hours=2)).isoformat(),
        "next_step": "fix the links",
    }
    text = am.limits_text(stop, tz=UTC, now=NOW)
    assert "Stopped at the 5-hour limit: 91.5% used, the ceiling is 90%." in text
    assert "The window resets 5:00 pm today." in text
    assert "Next step: fix the links" in text


def test_run_number_needs_two_runs() -> None:
    def run(number: int) -> dict[str, Any]:
        return {"id": number, "title": "T", "status": "done", "state": "completed"}

    one = am.Detail.model_validate(item() | {"lineage": [run(7)]})
    assert am.run_number(one) is None
    two = am.Detail.model_validate(item() | {"lineage": [run(4), run(7)]})
    assert am.run_number(two) == (2, 4)


# -- comments on the definition of a saved assignment -------------------------

DEFINITION = "title: Clean up the docs\ngoal: |-\n  Clean up the docs.\nmodel: sonnet"


def comment(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 11,
        "target": "definition",
        "line_no": 4,
        "line_text": "model: sonnet",
        "body": "Use opus <script>alert(1)</script>",
        "created_at": _iso(timedelta(minutes=10)),
        "resolved_at": None,
        "resolution": None,
        "stale": False,
    }
    return base | fields


class CommentManager(FakeManager):
    """A fake manager that also takes the comment writes. ``write`` is the
    status of each write."""

    def __init__(self, routes: dict[str, Any], write: int = 201, error: str = "") -> None:
        super().__init__(routes)
        self.write = write
        self.error = error

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method in ("POST", "DELETE"):
            self.requests.append(request)
            if self.write in (201, 204):
                return httpx.Response(self.write, json={} if self.write == 201 else None)
            return httpx.Response(self.write, json={"error": self.error})
        return super().handler(request)


def _csrf(html: str) -> str:
    start = html.index('name="csrf" value="') + len('name="csrf" value="')
    return html[start : html.index('"', start)]


def _client() -> TestClient:
    return TestClient(viewer.build_app(), follow_redirects=False)


def _unit(html: str, line: int) -> str:
    """The HTML of the unit of the definition whose first line is ``line``."""
    start = html.index(f'id="L{line}">')
    ends = [i for i in (html.find(m, start) for m in ('<div class="a-unit ', "a-source")) if i > 0]
    return html[start : min(ends, default=len(html))]


def _saved_html(monkeypatch, definition: str, comments: list[Any] | None = None) -> str:
    body = saved_item(open_comments=len(comments or [])) | {
        "runs_list": [],
        "definition": definition,
        "comments": comments or [],
    }
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    return _main(_get("/assignments/saved/3").text)


def test_the_definition_is_a_document_with_a_form_per_unit(data, monkeypatch) -> None:
    comments = [
        comment(),
        comment(id=12, line_no=1, line_text="title: Old", stale=True, body="Rename"),
        comment(
            id=13,
            line_no=2,
            body="Shorter",
            resolved_at=_iso(timedelta(minutes=1)),
            resolution="Made it <b>short</b>.",
        ),
        comment(id=14, line_no=40, stale=True, body="Gone line"),
    ]
    html = _saved_html(monkeypatch, DEFINITION, comments)
    assert "<table" not in html
    assert '<details class="a-def" open id="definition"><summary>Definition' in html
    assert "3 comments for Joshua" in html
    # The fields are properties, each a unit at its line; the goal is prose.
    assert '<div class="a-unit a-prop has-comments" id="L1">' in html
    assert '<span class="a-prop-key">title</span>' in html
    assert '<span class="a-prop-text">Clean up the docs</span>' in html
    assert '<div class="a-unit a-prop has-comments" id="L4">' in html
    assert '<div class="a-unit a-goal-head" id="L2">' in html
    assert '<div class="a-unit a-block p d0" id="L3"><div class="a-unit-body">' in html
    assert "<p>Clean up the docs.</p>" in html
    # Four units and four lines of the source: each has a "+".
    assert html.count('<details class="a-add">') == 4 + len(DEFINITION.split("\n"))
    assert 'action="/assignments/saved/3/comments"' in html
    # The source gives each line with its number, its text, and a "+".
    source = html[html.index('<details class="a-source">') :]
    for n, line in enumerate(DEFINITION.split("\n"), start=1):
        assert f'id="L{n}"' in html
        assert f'<a class="a-ln" href="#S{n}">{n}</a>' in source
        assert f'<input type="hidden" name="line_no" value="{n}">' in source
        assert f'<input type="hidden" name="line_text" value="{line}">' in source
    assert '<code class="a-code">  Clean up the docs.</code>' in source
    assert '<li class="a-line has-comments" id="S4">' in source
    # The body of a comment is text: escaped, never markup.
    assert "<script>alert(1)" not in html
    assert "Use opus &lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The open comment is under its field, with a Delete form, and shows once.
    assert 'id="C11"' in _unit(html, 4)
    assert html.count('id="C11"') == 1
    assert 'action="/assignments/saved/3/comments/11/delete"' in html
    # A stale comment says so and shows the line it was on.
    stale = html[html.index('id="C12"') :]
    assert "line changed" in stale[:600] and "title: Old" in stale[:600]
    # A comment on a line that is gone is still shown.
    assert "On lines that are gone" in html and 'id="C14"' in html
    # A resolved comment folds into the resolved group with the resolution.
    resolved = html[html.index('<details class="a-resolved">') :]
    assert "1 resolved" in resolved and 'id="C13"' in resolved
    assert "Made it &lt;b&gt;short&lt;/b&gt;." in resolved
    assert 'action="/assignments/saved/3/comments/13/delete"' not in html


def test_an_older_manager_with_no_definition_gets_no_form(data, monkeypatch) -> None:
    body = saved_item() | {"runs_list": []}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    html = _main(_get("/assignments/saved/3").text)
    assert "<pre><code>" in html and "goal: Clean up the docs." in html
    assert '<details class="a-add">' not in html


def test_the_saved_pages_show_the_comments_and_the_count(data, monkeypatch) -> None:
    body = saved_item(open_comments=1) | {
        "runs_list": [],
        "definition": DEFINITION,
        "comments": [comment()],
    }
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/saved/3": body,
                "/v1/saved": {"items": [saved_item(open_comments=2), saved_item(id=4, name="b")]},
            }
        ),
    )
    html = _main(_get("/assignments/saved/3").text)
    assert 'action="/assignments/saved/3/comments"' in html
    assert 'action="/assignments/saved/3/comments/11/delete"' in html
    assert "1 comment for Joshua" in html
    listed = _main(_get("/assignments/saved").text)
    assert listed.count("for Joshua") == 1 and "2 comments for Joshua" in listed


def test_a_comment_round_trip(data, monkeypatch) -> None:
    body = saved_item() | {"runs_list": [], "definition": DEFINITION}
    manager = _with(monkeypatch, CommentManager({"/v1/saved/3": body}))
    client = _client()
    csrf = _csrf(client.get("/assignments/saved/3", auth=ALEX).text)
    form = {"csrf": csrf, "line_no": "3", "line_text": "  Clean up the docs.", "body": "Say why."}
    response = client.post("/assignments/saved/3/comments", data=form, auth=ALEX)
    assert response.status_code == 303
    assert response.headers["location"] == "/assignments/saved/3#L3"
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert sent.url.path == "/v1/saved/3/comments"
    assert sent.url.params["person"] == "alex"
    assert sent.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert json.loads(sent.content) == {
        "line_no": 3,
        "line_text": "  Clean up the docs.",
        "body": "Say why.",
    }

    manager.write = 204
    response = client.post(
        "/assignments/saved/3/comments/11/delete", data={"csrf": csrf}, auth=ALEX
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/assignments/saved/3#definition"
    [gone] = [r for r in manager.requests if r.method == "DELETE"]
    assert gone.url.path == "/v1/saved/3/comments/11"
    assert gone.url.params["person"] == "alex"


def test_a_comment_needs_the_csrf_token_of_its_definition(data, monkeypatch) -> None:
    manager = _with(monkeypatch, CommentManager({}))
    client = _client()
    form = {"line_no": "1", "line_text": "x", "body": "y"}
    other = viewer._csrf_token("alex", "comments:/assignments/saved/4")
    for token in ("", "nope", other):
        response = client.post(
            "/assignments/saved/3/comments", data=form | {"csrf": token}, auth=ALEX
        )
        assert response.status_code == 403
    good = viewer._csrf_token("alex", viewer.saved_scope(3))
    response = client.post(
        "/assignments/saved/3/comments",
        data=form | {"csrf": good},
        headers={"Origin": "https://elsewhere.example"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert manager.requests == []


def test_a_comment_write_needs_a_sign_in(data, monkeypatch) -> None:
    manager = _with(monkeypatch, CommentManager({}))
    client = TestClient(viewer.build_app())
    for path in ("/assignments/saved/3/comments", "/assignments/saved/3/comments/1/delete"):
        assert client.post(path, data={"csrf": "x"}).status_code == 401
    assert manager.requests == []


@pytest.mark.parametrize(
    ("write", "error", "status", "text"),
    [
        (404, "not found", 404, "not found"),
        (400, "body is <b>bad</b>", 400, "body is &lt;b&gt;bad&lt;/b&gt;"),
        (409, "a resolved comment stays", 400, "a resolved comment stays"),
        (503, "", 400, "The manager refused the comment."),
    ],
)
def test_a_refused_comment_says_why(data, monkeypatch, write, error, status, text) -> None:
    _with(monkeypatch, CommentManager({}, write=write, error=error))
    good = viewer._csrf_token("alex", viewer.saved_scope(3))
    response = _client().post(
        "/assignments/saved/3/comments",
        data={"csrf": good, "line_no": "x", "line_text": "", "body": ""},
        auth=ALEX,
    )
    assert response.status_code == status
    assert text in response.text


def test_a_comment_when_the_manager_is_down_or_not_configured(data, monkeypatch) -> None:
    good = viewer._csrf_token("alex", viewer.saved_scope(3))
    form = {"csrf": good, "line_no": "1", "line_text": "x", "body": "y"}
    assert _client().post("/assignments/saved/3/comments", data=form, auth=ALEX).status_code == 404

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", VIEWER_TOKEN, transport=transport),
    )
    monkeypatch.setattr("joshua_shared.http.BACKOFF_BASE_S", 0.0)
    response = _client().post("/assignments/saved/3/comments", data=form, auth=ALEX)
    assert response.status_code == 400
    assert "did not answer" in response.text


# -- the definition as a document ---------------------------------------------

# The definition that the manager renders for {"title": "Clean up the docs",
# "goal": <the goal below>, "model": "sonnet", "budget": {"turns": 40},
# "repos": ["example/docs"], "dry_run": False}.
FULL_DEFINITION = (
    "title: Clean up the docs\ngoal: |-\n  # Clean up\n\n  Read each page\n"
    "  and fix the links.\n\n  - flat one\n  - parent\n    - nested one\n"
    "    - nested two\n  - last\n\n  1. first\n  2. second\n\n  > a quote\n\n"
    "  ```sh\n  make test\n  ```\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |\n"
    "model: sonnet\nbudget:\n  turns: 40\nrepos:\n  - example/docs\ndry_run: false"
)


def _form_of(unit: str) -> tuple[str, str]:
    """The ``line_no`` and the ``line_text`` that the form of a unit posts."""
    no = unit[unit.index('name="line_no" value="') + 22 :]
    text = unit[unit.index('name="line_text" value="') + 24 :]
    return no[: no.index('"')], text[: text.index('"')]


def test_each_block_of_the_goal_has_its_own_plus_at_its_first_line(data, monkeypatch) -> None:
    html = _saved_html(monkeypatch, FULL_DEFINITION)
    doc = html[: html.index('<details class="a-source">')]
    lines = FULL_DEFINITION.split("\n")
    expected = {
        2: "a-goal-head",
        3: "a-block h d0",
        5: "a-block p d0",
        8: "a-block li d0",
        9: "a-block li d0",
        10: "a-block li d1",
        11: "a-block li d1",
        12: "a-block li d0",
        14: "a-block li d0",
        15: "a-block li d0",
        17: "a-block quote d0",
        19: "a-block code d0",
        23: "a-block table d0",
    }
    for line, css in expected.items():
        assert f'<div class="a-unit {css}" id="L{line}">' in doc, line
        assert _form_of(_unit(doc, line)) == (str(line), escape(lines[line - 1]))
    # The nested item is a block of its own: one comment, one bullet.
    assert _form_of(_unit(doc, 10)) == ("10", "    - nested one")
    assert "<li>nested one</li>" in _unit(doc, 10) and "nested two" not in _unit(doc, 10)
    assert "<h1>Clean up</h1>" in doc and '<ol start="2">' in doc and "<table>" in doc
    # Every line of the text has one anchor, so the page can open at any line.
    for n in range(1, len(lines) + 1):
        assert doc.count(f'id="L{n}"') == 1, n
    # Six fields and thirteen units of the goal, then one "+" a line in the source.
    assert html.count('<details class="a-add">') == 5 + 13 + len(lines)


def test_the_property_rows_anchor_to_the_first_line_of_their_field(data, monkeypatch) -> None:
    html = _saved_html(monkeypatch, FULL_DEFINITION)
    doc = html[: html.index('<details class="a-source">')]
    props = doc[: doc.index('<div class="a-goal')]
    # The fields come first, in the order of the text; the goal is not one.
    order = [props.index(f'id="L{n}"') for n in (1, 26, 27, 29, 31)]
    assert order == sorted(order) and ">goal<" not in props
    assert _form_of(_unit(doc, 1)) == ("1", "title: Clean up the docs")
    assert _form_of(_unit(doc, 26)) == ("26", "model: sonnet")
    assert _form_of(_unit(doc, 27)) == ("27", "budget:")
    assert '<span class="a-anchor" id="L28"></span>' in _unit(doc, 27)
    assert '<span class="a-tag">turns: 40</span>' in _unit(doc, 27)
    assert '<span class="a-tag">example/docs</span>' in _unit(doc, 29)
    assert '<span class="a-prop-key">dry run</span>' in _unit(doc, 31)
    assert '<span class="a-prop-text">no</span>' in _unit(doc, 31)


def test_a_comment_inside_a_block_shows_under_it_with_its_line(data, monkeypatch) -> None:
    comments = [
        comment(id=21, line_no=6, line_text="  and fix the links.", body="Which links?"),
        comment(id=22, line_no=5, line_text="  Read each page", body="All of them?"),
        comment(id=23, line_no=28, line_text="  turns: 40", body="More turns."),
        comment(id=24, line_no=21, line_text="  make it", stale=True, body="Old"),
    ]
    html = _saved_html(monkeypatch, FULL_DEFINITION, comments)
    paragraph = _unit(html, 5)
    assert 'class="a-unit a-block p d0 has-comments"' in html
    assert 'id="C21"' in paragraph and 'id="C22"' in paragraph
    # A comment on a later line of the block quotes that line.
    c21 = paragraph[paragraph.index('id="C21"') :]
    assert '<span class="j-faint">Line 6:</span> <code>  and fix the links.</code>' in c21
    c22 = paragraph[paragraph.index('id="C22"') : paragraph.index('id="C21"')]
    assert "Line 5:" not in c22
    assert 'id="C23"' in _unit(html, 27) and "Line 28:" in _unit(html, 27)
    # A stale comment in a code block has the mark and the old line.
    code = _unit(html, 19)
    assert 'id="C24"' in code and "line changed" in code and "make it" in code
    for cid in (21, 22, 23, 24):
        assert html.count(f'id="C{cid}"') == 1


def test_the_source_shows_every_line_with_its_plus(data, monkeypatch) -> None:
    html = _saved_html(monkeypatch, FULL_DEFINITION)
    source = html[html.index('<details class="a-source"><summary>Source</summary>') :]
    lines = FULL_DEFINITION.split("\n")
    assert source.count('<li class="a-line') == len(lines)
    for n, line in enumerate(lines, start=1):
        row = source[source.index(f'id="S{n}"') :]
        row = row[: row.index("</li>")]
        assert f'<code class="a-code">{escape(line)}</code>' in row
        assert _form_of(row) == (str(n), escape(line))


def test_a_definition_with_no_goal_is_only_properties(data, monkeypatch) -> None:
    html = _saved_html(monkeypatch, "model: opus\ntools:\n  - a\n  - b")
    assert '<div class="a-goal' not in html
    assert '<div class="a-unit a-prop" id="L1">' in html
    assert '<div class="a-unit a-prop" id="L2"><span class="a-anchor" id="L3"></span>' in html
    assert '<span class="a-tag">a</span><span class="a-tag">b</span>' in html


def test_an_empty_definition_says_so(data, monkeypatch) -> None:
    html = _saved_html(monkeypatch, "")
    assert "The definition is empty." in html
    assert '<details class="a-source"><summary>Source</summary><ol class="a-lines"></ol>' in html


def test_odd_values_of_a_definition(data, monkeypatch) -> None:
    text = (
        'goal: ""\nnotes: null\nmeta: {}\nlist: []\nbad: [x\nmax_turns: 40\n'
        'repos:\n  - null\n  - {"a": 1}'
    )
    html = _saved_html(monkeypatch, text)
    # A goal that is not a block scalar is a field.
    assert '<span class="a-prop-key">goal</span>' in _unit(html, 1)
    for line in (1, 2, 3, 4):
        assert '<span class="j-faint">none</span>' in _unit(html, line), line
    assert '<code class="a-code">bad: [x</code>' in _unit(html, 5)
    assert '<span class="a-prop-key">max turns</span>' in _unit(html, 6)
    assert '<span class="a-tag">none</span><span class="a-tag">{&quot;a&quot;: 1}</span>' in html


def test_a_hostile_definition_is_escaped(data, monkeypatch) -> None:
    text = (
        'title: "<img src=x onerror=alert(1)>"\ngoal: |-\n  <script>alert(1)</script>\n\n'
        '  [x](javascript:alert(1))\nrepos:\n  - "<b>repo</b>"\n"<i>key</i>": 1'
    )
    html = _saved_html(monkeypatch, text)
    for bad in ("<script>alert", "<img src=x", "<b>repo", "<i>key", 'href="javascript:'):
        assert bad not in html, bad
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_the_definition_fits_a_phone() -> None:
    """No part of the definition is wider than a phone: the property rows go to
    one column, the text wraps, and a code block or a table scrolls in its own
    box."""
    css = (Path(viewer.__file__).parent / "viewer.css").read_text()
    ui = (Path(viewer.__file__).parent / "ui" / "joshua-ui.css").read_text()
    phone = css[css.index("@media (max-width: 560px)") :]
    phone = phone[: phone.index("\n}\n")]
    assert ".a-prop > .a-unit-body { grid-template-columns: minmax(0, 1fr);" in phone
    assert ".a-unit > details.a-add > summary { left: 4px; }" in phone
    assert "details.a-add > summary { left: 30px; opacity: 0.6; }" in phone
    rule = css[css.index(".a-unit {") :]
    rule = rule[: rule.index("}")]
    assert "min-width: 0" in rule and "overflow-wrap: anywhere" in rule
    assert "minmax(0, 1fr)" in css[css.index(".a-prop > .a-unit-body {") :][:200]
    assert ".a-tags { display: flex; flex-wrap: wrap;" in css
    assert "overflow-x: auto" in ui[ui.index(".j-prose pre {") :][:300]
    assert "overflow-x: auto" in ui[ui.index(".j-prose table {") :][:200]
