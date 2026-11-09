"""Offline tests for the viewer's assignments section.

A MockTransport stands in for the assignments manager, so no network is
needed. The answers follow the read API contract of the manager.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
    # The list items give the line under each title: no detail is read.
    assert [r.url.path for r in manager.requests] == ["/v1/assignments"]


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
    states = [r.url.params.get("state") for r in manager.requests]
    assert states == ["in_progress", "completed"]
    assert '<span class="j-chip on" aria-current="true">Queued</span>' not in html


def test_the_default_is_completed_also_when_only_a_queued_one_exists(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=completed": {"items": []},
                "/v1/assignments?state=queued": {"items": [item(state="queued")]},
            }
        ),
    )
    html = _main(_get("/assignments").text)
    assert '<span class="j-chip on" aria-current="true">Completed</span>' in html
    assert "No assignment is completed." in html


@pytest.mark.parametrize("query", [{}, {"state": "queued"}])
def test_no_pill_is_selected_when_the_manager_does_not_answer(data, monkeypatch, query) -> None:
    _with(monkeypatch, FakeManager(status=503))
    html = _main(_get("/assignments", **query).text)
    assert 'class="j-chip on"' not in html
    assert "did not answer" in html
    for key, _ in am.STATES:
        assert f'href="/assignments?state={key}"' in html


def test_no_pill_is_selected_when_the_second_read_fails(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": []},
                "/v1/assignments?state=completed": 503,
            }
        ),
    )
    html = _main(_get("/assignments").text)
    assert 'class="j-chip on"' not in html
    assert "did not answer" in html


def test_the_headings_start_with_a_capital(data, monkeypatch) -> None:
    _with(
        monkeypatch,
        FakeManager(
            {
                "/v1/assignments?state=in_progress": {"items": [item()]},
                "/v1/assignments/7": detail(),
                "/v1/saved": {"items": []},
                "/v1/saved/3": saved_item() | {"runs_list": []},
            }
        ),
    )
    listed = _get("/assignments").text
    assert '<h1 class="j-doc-title">Assignments</h1>' in listed
    assert "<title>Assignments</title>" in listed
    assert '<a href="/assignments">Assignments</a>' in _get("/assignments/7").text
    saved = _get("/assignments/saved").text
    assert '<h1 class="j-doc-title">Saved assignments</h1>' in saved
    assert "<title>Saved assignments</title>" in saved
    one = _get("/assignments/saved/3").text
    assert '<a href="/assignments">Assignments</a>' in one
    assert '<a href="/assignments/saved">Saved</a>' in one


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


def test_the_detail_page_in_progress_is_a_document(data, monkeypatch) -> None:
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
    assert "<b>12k</b><span>tokens</span>" in html
    assert f"{am.exact(12345)} tokens: input 10" in html
    assert "<b>1h 20m</b><span>working time</span>" in html
    order = [
        '<section class="a-now"',
        "<h2>Plan</h2>",
        "<h2>Outputs so far</h2>",
        "<h2>Progress</h2>",
        '<span class="a-fold-title">Report</span>',
        "<summary>Definition</summary>",
    ]
    places = [html.index(mark) for mark in order]
    assert places == sorted(places)
    # The plan is Markdown, with each task box drawn.
    assert '<li class="a-task done"><span class="a-box" role="img" aria-label="done">' in html
    assert '<li class="a-task"><span class="a-box" role="img" aria-label="to do">' in html
    # The report so far is folded, as Markdown.
    assert "<strong>two</strong>" in html
    # The progress is newest first.
    assert html.index("fixed two links</p>") < html.index("read the pages</p>")
    # The definition is YAML in a closed details element.
    assert '<details class="a-def" id="definition"><summary>' in html
    assert "goal: Clean up the docs." in html
    assert "turns: 40" in html
    assert "<h2>Error</h2>" not in html


def test_the_progress_now_has_the_bar_the_next_step_and_the_last_checkpoint(
    data, monkeypatch
) -> None:
    plan = "# Plan\n\n- [x] read\n- [x] list\n- [ ] fix\n- [ ] **Next:** check *images*\n"
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail(plan=plan)}))
    html = _main(_get("/assignments/7").text)
    now = html[html.index('<section class="a-now"') :]
    now = now[: now.index("</section>")]
    assert '<span class="a-steps"><b>2</b> of 4 steps</span>' in now
    assert '<rect class="fill" width="50" height="6" rx="3"/>' in now
    assert "check <em>images</em>" in now
    assert "fixed two links" in now and ">5m ago</time>" in now


def test_a_plan_with_no_boxes_has_no_bar(data, monkeypatch) -> None:
    body = detail(plan="# Plan\n\n1. Read.\n2. Fix.\n", checkpoints=[])
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert "a-steps" not in html and '<svg class="a-meter' not in html
    assert '<section class="a-now"' not in html
    assert "<li>Read.</li>" in html


def test_the_outputs_link_a_url_and_show_other_refs_as_code(data, monkeypatch) -> None:
    body = detail(
        outputs=[
            {"kind": "issue", "ref": "https://git.example.com/i/1", "title": "Issue 1", "at": None},
            {"kind": "page", "ref": "homelab/notes.md", "title": "Notes", "at": None},
            {"kind": "x", "ref": "javascript:alert(1)", "title": "Bad", "at": None},
            {"kind": "y", "ref": "https://", "title": "", "at": None},
        ],
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert (
        '<a class="a-out-title" href="https://git.example.com/i/1" rel="noreferrer">Issue 1</a>'
        '<span class="a-out-ref">git.example.com</span>'
    ) in html
    assert (
        '<span class="a-out-title">Notes</span><code class="a-out-ref">homelab/notes.md</code>'
    ) in html
    assert 'href="javascript:' not in html
    assert '<code class="a-out-ref">javascript:alert(1)</code>' in html
    assert '<code class="a-out-title">https://</code>' in html
    assert 'href="https://"' not in html


def test_outputs_show_also_with_no_report(data, monkeypatch) -> None:
    body = detail(outputs=[{"kind": "task", "ref": "12", "title": "", "at": None}])
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert "<h2>Outputs so far</h2>" in html
    assert '<code class="a-out-title">12</code>' in html
    assert "<b>1</b><span>output</span>" in html


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
    assert '<details class="a-def" open id="definition"><summary>Definition</summary>' in html
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


# -- the report as the page ---------------------------------------------------

REPORTS = Path(__file__).parent / "fixtures" / "reports"

FULL_OUTPUTS = [
    {
        "kind": "merge request",
        "ref": "https://git.example.com/docs/-/merge_requests/4",
        "title": "Fix 42 dead links",
        "at": None,
    },
    {
        "kind": "merge request",
        "ref": "https://git.example.com/docs/-/merge_requests/5",
        "title": "Rename the setup pages",
        "at": None,
    },
    {"kind": "task", "ref": "tasks#12", "title": "Rewrite the backup page", "at": None},
    {"kind": "page", "ref": "homelab/docs-audit.md", "title": "Docs audit", "at": None},
    {"kind": "snapshot", "ref": "snap-1", "title": "", "at": None},
]


def done(report: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "done",
        "state": "completed",
        "finished_at": _iso(timedelta(minutes=12)),
        "report": report,
        "report_at": _iso(timedelta(minutes=12)),
    }
    return detail(**(base | fields))


def report_fixture(name: str) -> str:
    return (REPORTS / f"{name}.md").read_text()


def _page_of(monkeypatch, body: dict[str, Any]) -> tuple[str, httpx.Response]:
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    response = _get("/assignments/7")
    assert response.status_code == 200
    return _main(response.text), response


def _between(html: str, start: str, end: str) -> str:
    part = html[html.index(start) :]
    return part[: part.index(end)]


def test_a_full_report_is_the_page(data, monkeypatch) -> None:
    html, response = _page_of(monkeypatch, done(report_fixture("full"), outputs=FULL_OUTPUTS))
    assert "<table" not in html
    assert 'http-equiv="refresh"' not in response.text
    order = [
        '<header class="a-hero">',
        '<section class="a-highlights"',
        "<h2>Decisions for you</h2>",
        "<h2>Outputs</h2>",
        "<h2>What was asked</h2>",
        "<h2>What was done</h2>",
        "<h2>Follow-ups</h2>",
        '<footer class="a-foot">',
        '<span class="a-fold-title">Plan</span>',
        '<span class="a-fold-title">Progress</span>',
        "<summary>Definition</summary>",
    ]
    places = [html.index(mark) for mark in order]
    assert places == sorted(places)
    # The hero: the state, the title, the lede, and the numbers.
    hero = _between(html, '<header class="a-hero">', "</header>")
    assert '<span class="j-badge ok">done</span>' in hero
    assert "ended <time" in hero
    assert '<p class="a-lede">Your wiki has no dead links now' in hero
    for tile in (
        "<b>12k</b><span>tokens</span>",
        "<b>1h 20m</b><span>working time</span>",
        "<b>86</b><span>turns</span>",
        "<b>sonnet</b><span>model</span>",
        "<b>5</b><span>outputs</span>",
        "<b>2</b><span>decisions</span>",
    ):
        assert tile in hero, tile
    # The highlights are cards with the lead large.
    assert html.count('<li class="a-hl">') == 4
    assert '<strong class="a-hl-lead">42 dead links fixed</strong>' in html
    assert '<p class="a-hl-text">across 18 pages, in one merge request.</p>' in html
    assert "<h2>Highlights</h2>" not in html
    # The decisions: numbered, with a count, a way to answer, and a copy button.
    decide = _between(html, "<h2>Decisions for you</h2>", "</section>")
    assert '<span class="a-count">2 decisions</span>' in decide
    assert '<p class="a-q">Delete the old install page?</p>' in decide
    assert decide.count("Tell Joshua your answer.") == 2
    assert 'data-copy="Assignment 7, decision 2: Keep the redirects for a year?" hidden>' in decide
    # The outputs: grouped by kind, a note from the report by ref.
    outputs = _between(html, "<h2>Outputs</h2>", "</section>")
    assert '<span class="a-count">5 outputs</span>' in outputs
    assert outputs.count('<div class="a-group">') == 4
    assert 'merge request <span class="j-count">2</span>' in outputs
    assert '<p class="a-out-note">every link fix, one commit a page</p>' in outputs
    assert '<p class="a-out-note">the full list of what I checked</p>' in outputs
    assert '<code class="a-out-title">snap-1</code>' in outputs
    # What was asked is a short quote, open.
    assert '<blockquote class="j-prose a-quote"><p>Clean up the docs: fix' in html
    # The follow-ups are a static checklist.
    assert html.count('<li><span class="a-box" aria-hidden="true"></span>') == 2
    # The cost at the foot has the full numbers.
    foot = _between(html, '<footer class="a-foot">', "</footer>")
    assert f"<b>{am.exact(12345)}</b><span>tokens in all</span>" in foot
    assert "<b>40</b><span>cache write</span>" in foot
    assert "<b>86</b><span>turns</span>" in foot
    # The plan folds with its bar in the summary.
    plan = _between(html, '<span class="a-fold-title">Plan</span>', "</summary>")
    assert '<svg class="a-meter mini"' in plan and "1 of 2 steps" in plan
    assert '<details class="a-fold" open' not in html
    # The one script copies the decisions, and the policy allows it by hash.
    assert html.count("<script>") == 1
    assert f"<script>{am.COPY_SCRIPT}</script>" in html
    digest = hashlib.sha256(am.COPY_SCRIPT.encode()).digest()
    assert am.COPY_SCRIPT_HASH == "sha256-" + base64.b64encode(digest).decode()
    assert f"script-src '{am.COPY_SCRIPT_HASH}'" in response.headers["content-security-policy"]


def test_a_report_with_no_highlights_skips_the_cards(data, monkeypatch) -> None:
    outputs = [
        {
            "kind": "issue",
            "ref": "https://git.example.com/ops/-/issues/1",
            "title": "Pin the web image",
            "at": None,
        }
    ]
    html, _ = _page_of(monkeypatch, done(report_fixture("no_highlights"), outputs=outputs))
    assert "a-highlights" not in html
    # "None." gives no section and no script.
    assert "<h2>Decisions for you</h2>" not in html
    assert "<h2>Follow-ups</h2>" not in html
    assert "<script" not in html
    assert "<b>0</b><span>decisions</span>" in html
    assert "<b>22</b><span>turns</span>" in html
    # The recorded outputs are the cards; a line that names no recorded ref is no card.
    assert '<span class="a-count">1 output</span>' in html
    assert "Pin the worker image" not in html
    assert '<p class="a-out-note">the web chart</p>' in html
    # An unknown heading goes under What was done.
    done_part = _between(html, "<h2>What was done</h2>", "</section>")
    assert "<p>I read each chart.</p>" in done_part
    assert "<h2>Notes</h2>" in done_part and "read the tags twice" in done_part


def test_a_report_with_none_of_the_headings_is_plain_markdown(data, monkeypatch) -> None:
    outputs = [{"kind": "page", "ref": "docs/x.md", "title": "X", "at": None}]
    html, _ = _page_of(monkeypatch, done(report_fixture("plain"), outputs=outputs))
    assert '<header class="a-hero">' in html
    assert "a-lede" not in html
    report = _between(html, "<h2>Report</h2>", "</section>")
    assert "<p>I looked at the docs. Everything is fine.</p>" in report
    assert "<li>one page needs a picture</li>" in report
    assert "## Highlights" in report
    assert "a-highlights" not in html
    assert "<h2>Outputs</h2>" in html
    assert '<span class="a-fold-title">Plan</span>' in html


def test_a_generated_stop_report_is_the_page(data, monkeypatch) -> None:
    body = done(
        report_fixture("generated"),
        status="failed",
        error="http_500",
        outputs=[],
        plan="# Plan\n\n- [x] Clone.\n- [x] List.\n- [ ] **Next:** Read.\n- [ ] File.\n",
    )
    html, _ = _page_of(monkeypatch, body)
    assert '<p class="a-lede generated">Generated by the runner at failed' in html
    assert '<strong class="a-hl-lead">Stopped at failed</strong>' in html
    assert '<strong class="a-hl-lead">2 of 4 steps</strong>' in html
    assert 'data-copy="Assignment 7, decision 1: Which project gets the issues?"' in html
    # With no recorded output, the lines of the section are the cards.
    outputs = _between(html, "<h2>Outputs</h2>", "</section>")
    assert '<span class="a-out-title">First</span><code class="a-out-ref">ops#1</code>' in outputs
    assert '<code class="a-out-title">notes/charts.txt</code>' in outputs
    assert "<b>9</b><span>turns</span>" in html
    assert '<span class="j-badge bad">failed</span>' in html
    assert html.index("<summary>Definition</summary>") < html.index("<h2>Error</h2>")


def test_an_unknown_kind_gets_the_other_icon() -> None:
    assert am.kind_icon("snapshot") == "other"
    assert am.kind_icon("") == "other"
    assert am.kind_icon("summary") == "other"
    assert am.kind_icon("MR") == "merge request"
    assert am.kind_icon("GitHub pull request") == "merge request"
    assert am.kind_icon("Wiki page") == "page"
    assert am.kind_icon("journal entry") == "journal"
    assert am.kind_icon("bug") == "issue"
    assert am.kind_icon("attachment") == "file"
    assert am.kind_icon("todo") == "task"
    other = am.output_card(am.Card("snapshot", "s-1", ""))
    assert am._KIND_ICONS["other"] in other


def test_a_card_with_only_a_note() -> None:
    card = am.output_card(am.Card("", "", "", "a *line* with no kind"))
    assert '<div class="a-out-main"><p class="a-out-note">a <em>line</em> with no kind</p>' in card


def test_a_long_ask_is_folded(data, monkeypatch) -> None:
    goal = "\n".join(f"Line {n} of the goal." for n in range(8))
    report = f"# T\n\nLede.\n\n## What was asked\n\n{goal}\n\n## What was done\n\nx\n"
    html, _ = _page_of(monkeypatch, done(report))
    assert '<details class="a-fold"><summary><span class="a-fold-title">What was asked' in html
    assert '<span class="a-fold-hint">Line 0 of the goal. Line 1' in html
    assert "<h2>What was asked</h2>" not in html


def test_sections_with_text_and_no_items(data, monkeypatch) -> None:
    report = (
        "# T\n\nLede.\n\n## Highlights\n\nIt went well.\n\n"
        "## Decisions for you\n\nPick a name for the folder.\n\n"
        "## Follow-ups\n\nAsk the team.\n"
    )
    html, _ = _page_of(monkeypatch, done(report))
    assert '<section class="a-highlights" aria-label="Highlights"><div class="j-prose">' in html
    decide = _between(html, "<h2>Decisions for you</h2>", "</section>")
    assert "<p>Pick a name for the folder.</p>" in decide and "a-count" not in decide
    assert "<p>Ask the team.</p>" in _between(html, "<h2>Follow-ups</h2>", "</section>")
    assert "<script" not in html
    assert "<h2>What was done</h2>" not in html


def test_a_highlight_with_no_lead_is_plain_text(data, monkeypatch) -> None:
    report = "# T\n\n## Highlights\n\n- **Lead** text\n- no lead here\n"
    html, _ = _page_of(monkeypatch, done(report, plan=None, checkpoints=[]))
    assert '<li class="a-hl"><p class="a-hl-text">no lead here</p></li>' in html
    assert '<strong class="a-hl-lead">Lead</strong>' in html
    assert "a-lede" not in html
    assert '<span class="a-fold-title">Plan</span>' not in html
    assert '<span class="a-fold-title">Progress</span>' not in html


def test_a_hostile_report_is_escaped(data, monkeypatch) -> None:
    report = (
        '# <script>x</script>\n\n<img src=x onerror="a()"> lede\n\n'
        "## Highlights\n\n- **<u>1</u>** <script>alert(1)</script>\n\n"
        "## Decisions for you\n\n"
        '1. **Pick "a" <b>?</b>** [x](javascript:alert(2))\n\n'
        "## Outputs\n\n- issue: [t](javascript:alert(3)) — <i>note</i>\n"
    )
    outputs = [{"kind": "<k>", "ref": 'https://example.com/"><x', "title": "<t>", "at": None}]
    html, _ = _page_of(monkeypatch, done(report, outputs=outputs))
    without_script = html.replace(f"<script>{am.COPY_SCRIPT}</script>", "")
    assert "<script" not in without_script
    assert "<img" not in html and "<u>" not in html and "<i>note" not in html
    assert 'href="javascript:' not in html
    assert 'data-copy="Assignment 7, decision 1: Pick &quot;a&quot; &lt;b&gt;?&lt;/b&gt;"' in html
    assert 'href="https://example.com/&quot;&gt;&lt;x"' in html
    assert "&lt;k&gt;" in html and "&lt;t&gt;" in html


def test_a_completed_assignment_with_no_report_shows_its_plan(data, monkeypatch) -> None:
    html, _ = _page_of(monkeypatch, detail(status="cancelled", state="completed"))
    assert "<h2>Plan</h2>" in html
    assert "a-lede" not in html


# -- the line under a row ------------------------------------------------------


def test_a_row_has_the_lede_or_the_next_step_and_the_outputs(data, monkeypatch) -> None:
    rows = [
        item(
            id=1,
            title="Finished",
            status="done",
            state="completed",
            lede="Your wiki has no dead links now <b>.</b>",
            next_step="not this",
            outputs_count=5,
        ),
        item(id=2, title="Running", status="failed", state="completed", next_step="`fix` it"),
        item(id=3, title="Missing", status="done", state="completed"),
    ]
    manager = _with(monkeypatch, FakeManager({"/v1/assignments?state=completed": {"items": rows}}))
    html = _main(_get("/assignments", state="completed").text)
    assert "<table" not in html
    first = _between(html, 'href="/assignments/1"', "</li>")
    assert '<span class="a-row-line">Your wiki has no dead links now &lt;b&gt;' in first
    assert "5 outputs" in first and "not this" not in first
    second = _between(html, 'href="/assignments/2"', "</li>")
    assert '<span class="a-row-line">Next: <code>fix</code> it</span>' in second
    assert "output" not in second
    third = _between(html, 'href="/assignments/3"', "</li>")
    assert "a-row-line" not in third
    # One read for the whole page: no read of a detail.
    assert [r.url.path for r in manager.requests] == ["/v1/assignments"]
    assert {r.url.params["person"] for r in manager.requests} == {"alex"}


def test_an_older_manager_with_no_list_fields_gives_plain_rows(data, monkeypatch) -> None:
    old = {k: v for k, v in item().items() if k not in ("lede", "next_step", "outputs_count")}
    _with(monkeypatch, FakeManager({"/v1/assignments?state=in_progress": {"items": [old]}}))
    html = _main(_get("/assignments").text)
    assert 'href="/assignments/7">Clean up the docs</a>' in html
    assert "a-row-line" not in html


def test_text_after_the_lede_goes_under_what_was_done(data, monkeypatch) -> None:
    report = "# T\n\nThe lede.\n\nA second paragraph.\n\n## What was done\n\nWork.\n"
    html, _ = _page_of(monkeypatch, done(report))
    done_part = _between(html, "<h2>What was done</h2>", "</section>")
    assert done_part.index("<p>Work.</p>") < done_part.index("<p>A second paragraph.</p>")
    assert '<p class="a-lede">The lede.</p>' in html


# -- comments on a definition -------------------------------------------------

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


def test_the_definition_has_numbered_lines_and_a_form_per_line(data, monkeypatch) -> None:
    body = detail(
        definition=DEFINITION,
        comments=[
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
        ],
        open_comments=3,
    )
    _with(monkeypatch, FakeManager({"/v1/assignments/7": body}))
    html = _main(_get("/assignments/7").text)
    assert "<table" not in html
    # Open comments open the definition, with the count at the top.
    assert '<details class="a-def" open id="definition"><summary>Definition' in html
    assert "3 comments for Joshua" in html
    # One row per line, each with its number, its text, and a "+".
    for n, line in enumerate(DEFINITION.split("\n"), start=1):
        assert f'id="L{n}"' in html
        assert f'<input type="hidden" name="line_text" value="{line}">' in html
        assert f'<a class="a-ln" href="#L{n}">{n}</a>' in html
        assert f'<input type="hidden" name="line_no" value="{n}">' in html
    assert '<code class="a-code">  Clean up the docs.</code>' in html
    assert html.count('<details class="a-add">') == 4
    assert 'action="/assignments/7/comments"' in html
    # The body of a comment is text: escaped, never markup.
    assert "<script>alert(1)" not in html
    assert "Use opus &lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The open comment is under its line, with a Delete form.
    line4 = html[html.index('id="L4"') : html.index("</li>", html.index('id="C11"'))]
    assert 'id="C11"' in line4
    assert 'action="/assignments/7/comments/11/delete"' in html
    # A stale comment says so and shows the line it was on.
    stale = html[html.index('id="C12"') :]
    assert "line changed" in stale[:600] and "title: Old" in stale[:600]
    # A comment on a line that is gone is still shown.
    assert "On lines that are gone" in html and 'id="C14"' in html
    # A resolved comment folds into the resolved group with the resolution.
    resolved = html[html.index('<details class="a-resolved">') :]
    assert "1 resolved" in resolved and 'id="C13"' in resolved
    assert "Made it &lt;b&gt;short&lt;/b&gt;." in resolved
    assert 'action="/assignments/7/comments/13/delete"' not in html


def test_with_no_open_comment_the_run_definition_is_closed(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail(definition=DEFINITION)}))
    html = _main(_get("/assignments/7").text)
    assert '<details class="a-def" id="definition"><summary>Definition</summary>' in html
    assert "for Joshua" not in html


def test_an_older_manager_with_no_definition_gets_no_form(data, monkeypatch) -> None:
    _with(monkeypatch, FakeManager({"/v1/assignments/7": detail()}))
    html = _main(_get("/assignments/7").text)
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


@pytest.mark.parametrize(
    ("page", "manager_path", "post_path"),
    [
        ("/assignments/7", "/v1/assignments/7", "/assignments/7/comments"),
        ("/assignments/saved/3", "/v1/saved/3", "/assignments/saved/3/comments"),
    ],
)
def test_a_comment_round_trip(data, monkeypatch, page, manager_path, post_path) -> None:
    body = (
        detail(definition=DEFINITION)
        if "saved" not in page
        else saved_item() | {"runs_list": [], "definition": DEFINITION}
    )
    manager = _with(monkeypatch, CommentManager({manager_path: body}))
    client = _client()
    csrf = _csrf(client.get(page, auth=ALEX).text)
    form = {"csrf": csrf, "line_no": "3", "line_text": "  Clean up the docs.", "body": "Say why."}
    response = client.post(post_path, data=form, auth=ALEX)
    assert response.status_code == 303
    assert response.headers["location"] == f"{page}#L3"
    [sent] = [r for r in manager.requests if r.method == "POST"]
    assert sent.url.path == f"{manager_path}/comments"
    assert sent.url.params["person"] == "alex"
    assert sent.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert json.loads(sent.content) == {
        "line_no": 3,
        "line_text": "  Clean up the docs.",
        "body": "Say why.",
    }

    manager.write = 204
    response = client.post(f"{post_path}/11/delete", data={"csrf": csrf}, auth=ALEX)
    assert response.status_code == 303
    assert response.headers["location"] == f"{page}#definition"
    [gone] = [r for r in manager.requests if r.method == "DELETE"]
    assert gone.url.path == f"{manager_path}/comments/11"
    assert gone.url.params["person"] == "alex"


def test_a_comment_needs_the_csrf_token_of_its_definition(data, monkeypatch) -> None:
    manager = _with(monkeypatch, CommentManager({"/v1/assignments/7": detail()}))
    client = _client()
    form = {"line_no": "1", "line_text": "x", "body": "y"}
    other = viewer._csrf_token("alex", "comments:/assignments/8")
    for token in ("", "nope", other):
        response = client.post("/assignments/7/comments", data=form | {"csrf": token}, auth=ALEX)
        assert response.status_code == 403
    good = viewer._csrf_token("alex", "comments:/assignments/7")
    response = client.post(
        "/assignments/7/comments",
        data=form | {"csrf": good},
        headers={"Origin": "https://elsewhere.example"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert manager.requests == []


def test_a_comment_write_needs_a_sign_in(data, monkeypatch) -> None:
    manager = _with(monkeypatch, CommentManager({}))
    client = TestClient(viewer.build_app())
    for path in ("/assignments/7/comments", "/assignments/saved/3/comments/1/delete"):
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
    good = viewer._csrf_token("alex", "comments:/assignments/7")
    response = _client().post(
        "/assignments/7/comments",
        data={"csrf": good, "line_no": "x", "line_text": "", "body": ""},
        auth=ALEX,
    )
    assert response.status_code == status
    assert text in response.text


def test_a_comment_when_the_manager_is_down_or_not_configured(data, monkeypatch) -> None:
    good = viewer._csrf_token("alex", "comments:/assignments/7")
    form = {"csrf": good, "line_no": "1", "line_text": "x", "body": "y"}
    assert _client().post("/assignments/7/comments", data=form, auth=ALEX).status_code == 404

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport = httpx.MockTransport(down)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", VIEWER_TOKEN, transport=transport),
    )
    monkeypatch.setattr("joshua_shared.http.BACKOFF_BASE_S", 0.0)
    response = _client().post("/assignments/7/comments", data=form, auth=ALEX)
    assert response.status_code == 400
    assert "did not answer" in response.text


def test_the_turns_tile_reads_turns_from_the_manager_first(data, monkeypatch) -> None:
    html, _ = _page_of(monkeypatch, done(report_fixture("full"), turns=120))
    assert "<b>120</b><span>turns</span>" in html
    assert "<b>86</b><span>turns</span>" not in html
    # Without turns from the manager, the Cost section of the report gives them.
    html, _ = _page_of(monkeypatch, done(report_fixture("full")))
    assert "<b>86</b><span>turns</span>" in html


def test_a_page_in_progress_shows_the_turns_of_the_manager(data, monkeypatch) -> None:
    html, _ = _page_of(monkeypatch, detail(turns=14))
    assert "<b>14</b><span>turns</span>" in html
    html, _ = _page_of(monkeypatch, detail())
    assert "<span>turns</span>" not in html
