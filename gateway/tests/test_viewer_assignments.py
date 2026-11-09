"""Offline tests for the viewer's assignments section.

A MockTransport stands in for the assignments manager, so no network is
needed. The answers follow the read API contract of the manager.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
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


def detail(**fields: Any) -> dict[str, Any]:
    base = item() | {
        "spec": {"goal": "Clean up the docs.", "model": "sonnet", "budget": {"turns": 40}},
        "plan": "# Plan\n\n- [x] read the pages\n- [ ] fix the links\n",
        "plan_at": _iso(timedelta(minutes=5)),
        "report": None,
        "report_at": None,
        "outputs": [],
        "checkpoints": [
            {"at": _iso(timedelta(minutes=5)), "note": "fixed two links"},
            {"at": _iso(timedelta(minutes=50)), "note": "read the pages"},
        ],
        "summary": None,
        "follow_ups": None,
        "answer": None,
        "error": None,
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
    html = _get("/").text
    nav = html[html.index('<nav class="v-sections"') :]
    nav = nav[: nav.index("</nav>")]
    assert nav.index('href="/usage"') < nav.index('href="/assignments"')
    assert '<svg class="j-icon"' in nav[nav.index('href="/assignments"') :]
    assert '<a class="j-tab" href="/assignments">Assignments</a>' in html


def test_the_current_section_is_marked(data) -> None:
    html = _get("/assignments").text
    assert 'class="j-nav-item" href="/assignments" aria-current="page"' in html
    assert 'class="j-tab" href="/assignments" aria-current="page"' in html
    assert "<strong>Assignments</strong>" in html


@pytest.mark.parametrize(
    "path", ["/assignments", "/assignments/7", "/assignments/saved", "/assignments/saved/3"]
)
def test_each_page_needs_a_sign_in(data, path) -> None:
    assert TestClient(viewer.build_app()).get(path).status_code == 401


# -- not configured, and a manager that does not answer -----------------------


@pytest.mark.parametrize(
    "path", ["/assignments", "/assignments/7", "/assignments/saved", "/assignments/saved/3"]
)
def test_without_the_manager_each_page_says_it_is_not_configured(data, path) -> None:
    response = _get(path)
    assert response.status_code == 200
    assert "not configured" in response.text
    assert am.ASSIGNMENTS_URL_ENV in response.text


@pytest.mark.parametrize(
    "path", ["/assignments", "/assignments/7", "/assignments/saved", "/assignments/saved/3"]
)
def test_a_manager_that_fails_gives_a_page_with_nothing_old(data, monkeypatch, path) -> None:
    _with(monkeypatch, FakeManager(status=503))
    response = _get(path)
    assert response.status_code == 200
    assert "did not answer" in response.text
    assert "j-list-row" not in response.text


def test_an_unknown_shape_is_a_failure(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": "nope"}}))
    assert "did not answer" in _get("/assignments").text


async def test_a_network_error_is_none() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = am.AssignmentsClient("http://assignments", "t", transport=httpx.MockTransport(fail))
    assert await client.list("alex", "queued") is None
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


# -- the person, the token, and the list -------------------------------------


def test_the_person_and_the_token_go_to_the_manager(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({"/v1/assignments?state=queued": {"items": []}}))
    _get("/assignments", state="queued")
    (request,) = manager.requests
    assert request.headers["Authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert request.url.params["person"] == "alex"
    assert request.url.params["state"] == "queued"
    assert request.url.params["limit"] == str(am.LIST_LIMIT)


def test_each_route_passes_the_person(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments/7": detail(),
                "/v1/saved": {"items": []},
                "/v1/saved/3": saved_item() | {"runs_list": []},
            }
        ),
    )
    for path in ("/assignments/7", "/assignments/saved", "/assignments/saved/3"):
        assert _get(path).status_code == 200, path
    assert [r.url.params["person"] for r in manager.requests] == ["alex", "alex", "alex"]
    assert [r.url.path for r in manager.requests] == [
        "/v1/assignments/7",
        "/v1/saved",
        "/v1/saved/3",
    ]


def test_the_default_is_in_progress_when_one_exists(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch,
        FakeManager({"/v1/assignments?state=in_progress": {"items": [item()]}}),
    )
    html = _main(_get("/assignments").text)
    assert '<span class="j-chip on" aria-current="true">In progress</span>' in html
    assert 'href="/assignments/7">Clean up the docs</a>' in html
    assert len(manager.requests) == 1


def test_the_default_is_completed_when_nothing_is_in_progress(data, monkeypatch) -> None:
    done = item(id=5, title="Old job", status="done", state="completed")
    manager = _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=completed": {"items": [done]},
            }
        ),
    )
    html = _main(_get("/assignments").text)
    assert '<span class="j-chip on" aria-current="true">Completed</span>' in html
    assert "Old job" in html
    assert [r.url.params["state"] for r in manager.requests] == ["in_progress", "completed"]


@pytest.mark.parametrize("state", ["queued", "in_progress", "completed"])
def test_each_state_has_its_pill_and_an_empty_text(data, monkeypatch, state) -> None:
    _with(monkeypatch, FakeManager({f"/v1/assignments?state={state}": {"items": []}}))
    html = _main(_get("/assignments", state=state).text)
    label = dict(am.STATES)[state]
    assert f'<span class="j-chip on" aria-current="true">{label}</span>' in html
    assert f"No assignment is {label.lower()}." in html
    for other, _ in am.STATES:
        if other != state:
            assert f'href="/assignments?state={other}"' in html
    assert 'href="/assignments/saved">Saved</a>' in html


def test_an_unknown_state_gives_the_default(data, monkeypatch) -> None:
    manager = _with(
        monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [item()]}})
    )
    _get("/assignments", state="nope")
    assert manager.requests[0].url.params["state"] == "in_progress"


def test_a_row_has_the_status_the_time_the_model_and_the_tokens(data, monkeypatch) -> None:
    asking = item(id=8, title="Ask me", status="waiting", open_question="Which repo?")
    _with(
        monkeypatch,
        FakeManager({"/v1/assignments?state=in_progress": {"items": [item(), asking]}}),
    )
    html = _main(_get("/assignments").text)
    assert "<table" not in html
    assert html.index("Clean up the docs") < html.index("Ask me")
    assert '<span class="j-badge accent">running</span>' in html
    assert '<span class="j-badge warn">waiting</span>' in html
    assert "started <time" in html and ">2h ago</time>" in html
    assert 'title="2026-11-09 13:00 UTC"' in html
    assert "sonnet" in html
    assert "12k tokens" in html
    assert html.count(">question</span>") == 1


def test_a_hostile_title_and_model_are_escaped(data, monkeypatch) -> None:
    bad = item(title="<script>alert(1)</script>", model='"><img src=x>')
    _with(monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [bad]}}))
    html = _get("/assignments").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html
    assert '"><img src=x>' not in html


# -- the detail page ---------------------------------------------------------


def test_the_detail_page_is_a_document(data, monkeypatch) -> None:
    body = detail(
        saved={"id": 3, "name": "docs-cleanup"},
        report="# Clean up the docs\n\n## What was done\n\nFixed **two** links.\n",
        report_at=_iso(timedelta(minutes=1)),
        outputs=[
            {
                "kind": "merge request",
                "ref": "https://git.example.com/mr/4",
                "title": "MR 4",
                "at": None,
            },
            {"kind": "task", "ref": "tasks#12", "title": "Follow up", "at": None},
        ],
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    response = _get("/assignments/7")
    assert response.status_code == 200
    html = _main(response.text)
    assert "<table" not in html
    assert "<script" not in response.text
    assert '<h1 class="j-doc-title">Clean up the docs</h1>' in html
    assert 'href="/assignments/saved/3">saved: docs-cleanup</a>' in html
    assert f"{am.exact(12345)} tokens" in html and "1h 20m working" in html
    order = [
        "<h2>Plan</h2>",
        "<h2>Report</h2>",
        "<h2>Progress</h2>",
        "<summary>Definition</summary>",
    ]
    places = [html.index(mark) for mark in order]
    assert places == sorted(places)
    # The plan and the report are Markdown.
    assert "<li>[x] read the pages</li>" in html
    assert "<strong>two</strong>" in html
    # The progress is newest first.
    assert html.index("fixed two links") < html.index("read the pages</p>")
    # The definition is YAML in a closed details element.
    assert '<details class="a-def"><summary>' in html
    assert "goal: Clean up the docs." in html
    assert "turns: 40" in html
    assert "<h2>Error</h2>" not in html


def test_the_outputs_link_a_url_and_show_other_refs_as_code(data, monkeypatch) -> None:
    body = detail(
        report="Done.",
        outputs=[
            {"kind": "issue", "ref": "https://git.example.com/i/1", "title": "Issue 1", "at": None},
            {"kind": "page", "ref": "homelab/notes.md", "title": "Notes", "at": None},
            {"kind": "x", "ref": "javascript:alert(1)", "title": "Bad", "at": None},
        ],
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert (
        '<li><span class="a-kind">issue</span>: '
        '<a href="https://git.example.com/i/1" rel="noreferrer">Issue 1</a></li>'
    ) in html
    assert '<li><span class="a-kind">page</span>: Notes <code>homelab/notes.md</code></li>' in html
    assert 'href="javascript:' not in html
    assert "<code>javascript:alert(1)</code>" in html


def test_outputs_show_also_with_no_report(data, monkeypatch) -> None:
    body = detail(outputs=[{"kind": "task", "ref": "12", "title": "", "at": None}])
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert "No report yet." in html
    assert '<span class="a-kind">task</span>: <code>12</code>' in html


def test_the_page_refreshes_only_while_in_progress(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({"/v1/assignments/7": detail()}))
    assert '<meta http-equiv="refresh" content="20">' in _get("/assignments/7").text
    for state, status in (("queued", "queued"), ("completed", "done")):
        manager.routes["/v1/assignments/7"] = detail(state=state, status=status)
        assert 'http-equiv="refresh"' not in _get("/assignments/7").text, state


def test_an_open_question_shows_a_notice(data, monkeypatch) -> None:
    body = detail(
        status="waiting", open_question="Which <b>branch</b>?", asked_at=_iso(timedelta(minutes=30))
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    notice = html[html.index('<aside class="a-notice warn"') : html.index("</aside>")]
    assert "Which &lt;b&gt;branch&lt;/b&gt;?" in notice
    assert ">30m ago</time>" in notice
    assert html.index("</aside>") < html.index("<h2>Plan</h2>")


def test_a_limits_stop_shows_a_notice(data, monkeypatch) -> None:
    stop = {
        "at": _iso(timedelta(minutes=10)),
        "window": "five_hour",
        "used_pct": 91.5,
        "ceiling": 90,
        "resets_at": (NOW + timedelta(hours=2)).isoformat(),
        "next_step": "fix the links",
    }
    body = detail(status="paused_limits", stopped_at_limits=stop)
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert "Paused at the subscription limits" in html
    assert "Stopped at the 5-hour limit: 91.5% used, the ceiling is 90%." in html
    assert "The window resets 5:00 pm today." in html
    assert "Next step: fix the links" in html
    assert '<span class="j-badge warn">paused at limits</span>' in html


def test_a_stop_on_unknown_limits_says_so() -> None:
    text = am._limits_text({"reason": "unknown"}, tz=UTC, now=NOW)
    assert text == "Stopped because the subscription limits are not known."


def test_the_error_shows_at_the_end(data, monkeypatch) -> None:
    body = detail(
        status="failed", state="completed", error="boom <x>", finished_at=_iso(timedelta(0))
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert '<pre class="a-error">boom &lt;x&gt;</pre>' in html
    assert html.index("<summary>Definition</summary>") < html.index("<h2>Error</h2>")
    assert '<span class="j-badge bad">failed</span>' in html
    assert "ended <time" in html


def test_raw_html_in_the_plan_and_the_report_is_text(data, monkeypatch) -> None:
    body = detail(
        plan="<script>alert(1)</script>\n\n[x](javascript:alert(2))",
        report="<img src=x onerror=alert(3)>",
        checkpoints=[{"at": None, "note": "<b>hi</b>"}],
        spec={"goal": "</code></pre><script>alert(4)</script>"},
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _get("/assignments/7").text
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert 'href="javascript:' not in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html


def test_an_empty_plan_says_so(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail(plan=None, checkpoints=[])}))
    html = _main(_get("/assignments/7").text)
    assert "No plan yet." in html
    assert "No checkpoint yet." in html
    assert "<h2>Report</h2>" not in html


def test_an_assignment_of_another_person_is_404(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({}))
    assert _get("/assignments/99").status_code == 404
    assert _get("/assignments/saved/99").status_code == 404


def test_a_path_that_is_not_a_number_is_404(data, monkeypatch) -> None:
    manager = _with(monkeypatch, FakeManager({}))
    assert _get("/assignments/abc").status_code == 404
    assert manager.requests == []


# -- the saved pages ---------------------------------------------------------


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


def test_the_saved_detail_has_the_definition_open_and_the_runs(data, monkeypatch) -> None:
    runs = [item(id=9, title="Second run"), item(id=7, title="First run", status="done")]
    body = saved_item(runs=1) | {"runs_list": runs}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    html = _main(_get("/assignments/saved/3").text)
    assert "<table" not in html
    assert '<h1 class="j-doc-title">docs-cleanup</h1>' in html
    assert "1 run ·" in html
    assert '<details class="a-def" open><summary>Definition</summary>' in html
    assert html.index('href="/assignments/9"') < html.index('href="/assignments/7"')


def test_a_saved_detail_with_no_run_says_so(data, monkeypatch) -> None:
    body = saved_item(runs=0, last_run=None) | {"runs_list": []}
    _with(monkeypatch, FakeManager({"/v1/saved/3": body}))
    assert "No run yet." in _get("/assignments/saved/3").text


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
    assert am.ago(seconds) == text


def test_duration() -> None:
    assert am.duration(-1) == "0s"
    assert am.duration(45) == "45s"
    assert am.duration(12 * 60) == "12m"
    assert am.duration(80 * 60) == "1h 20m"


def test_time_html_needs_an_offset() -> None:
    assert am.time_html(None, tz=UTC, now=NOW) == ""
    assert am.time_html("2026-11-09T10:00:00", tz=UTC, now=NOW) == ""
    assert am.time_html("not a time", tz=UTC, now=NOW) == ""


def test_is_url() -> None:
    assert am.is_url("https://git.example.com/x")
    assert am.is_url("http://example.com")
    assert not am.is_url("javascript:alert(1)")
    assert not am.is_url("https://")
    assert not am.is_url("tasks#12")
    assert not am.is_url("http://[bad")


def test_an_unknown_status_is_a_plain_chip() -> None:
    assert am.status_chip("odd_<x>") == '<span class="j-badge plain">odd &lt;x&gt;</span>'


def test_parse_state() -> None:
    assert am.parse_state("queued") == "queued"
    assert am.parse_state(None) is None
    assert am.parse_state("running") is None
