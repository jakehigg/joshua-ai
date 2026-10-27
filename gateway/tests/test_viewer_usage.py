"""Offline tests for the viewer's usage page.

A MockTransport stands in for core, so no network is needed. The fixture shape
is the same as in `test_viewer_search.py`.
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_usage as usage_mod
from joshua_shared import config
from joshua_shared.contracts import UsageResponse
from starlette.testclient import TestClient

ALEX_PW = "alex-secret"
ALEX = ("alex", ALEX_PW)
VIEWER_TOKEN = "viewer-token"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)


def _counts(calls: int, input_: int, output: int, read: int = 0, write: int = 0) -> dict:
    return {
        "calls": calls,
        "input": input_,
        "output": output,
        "cache_read": read,
        "cache_write": write,
        "total": input_ + output + read + write,
        "workers": {},
    }


ANSWER: dict[str, Any] = {
    "timezone": "America/New_York",
    "days": 30,
    "groups": ["chat", "extra"],
    "rows": [
        {
            "date": "2026-10-20",
            "groups": {
                "chat": _counts(12, 1000, 200, 30000, 400),
                "extra": _counts(5, 7000, 900),
            },
        },
        {"date": "2026-10-18", "groups": {"chat": _counts(2, 10, 5)}},
    ],
    "totals": {
        "chat": _counts(14, 1010, 205, 30000, 400),
        "extra": _counts(5, 7000, 900),
    },
}


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX_PW)
    monkeypatch.delenv(usage_mod.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(usage_mod.VIEWER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return root


class FakeCore:
    def __init__(self, answer: dict | None = None, status: int = 200) -> None:
        self.answer = answer if answer is not None else ANSWER
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        return httpx.Response(200, json=self.answer)


def _with_core(monkeypatch, core: FakeCore) -> None:
    transport = httpx.MockTransport(core.handler)
    monkeypatch.setattr(
        usage_mod,
        "from_env",
        lambda env: usage_mod.CoreUsage("http://core", VIEWER_TOKEN, transport=transport),
    )


def _get(**params: Any) -> httpx.Response:
    return TestClient(viewer.build_app()).get("/usage", params=params, auth=ALEX)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch) -> None:
    """The chart ends on today; the fixture answer ends on 2026-10-20."""
    monkeypatch.setattr(usage_mod, "_today", lambda timezone: date(2026, 10, 20))


def test_the_nav_links_the_usage_page(data) -> None:
    html = TestClient(viewer.build_app()).get("/", auth=ALEX).text
    nav = html[html.index("<nav>") : html.index("</nav>")]
    assert nav.index('href="/journal"') < nav.index('href="/usage"')


def test_the_page_needs_a_sign_in(data) -> None:
    assert TestClient(viewer.build_app()).get("/usage").status_code == 401


def test_without_core_the_page_says_it_is_not_configured(data) -> None:
    response = _get()
    assert response.status_code == 200
    assert "not configured" in response.text
    assert usage_mod.CORE_URL_ENV in response.text


def test_a_core_that_fails_still_gives_a_page(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore(status=503))
    response = _get()
    assert response.status_code == 200
    assert "Core did not answer" in response.text


def test_the_page_renders_the_tables_and_the_chart(data, monkeypatch) -> None:
    core = FakeCore()
    _with_core(monkeypatch, core)
    response = _get(days="7")
    html = response.text
    assert response.status_code == 200
    [request] = core.requests
    assert request.url.path == usage_mod.USAGE_PATH
    assert request.url.params["days"] == "7"
    assert request.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert "<svg" in html and "<script" not in html
    assert "<th>chat</th>" in html and "<th>extra</th>" in html
    # The table holds exact integers: the chat total of the 20th.
    assert usage_mod.exact(31600) in html
    # The totals column of the 20th: chat and extra added.
    assert usage_mod.exact(31600 + 7900) in html
    # The totals row: everything in the window.
    assert usage_mod.exact(31615 + 7900) in html
    # Three bars: two groups on the 20th, one on the 18th. The window is the
    # 30 days core answered with, so a slot is a thirtieth of the plot, and
    # the axis starts 29 days before today.
    assert html.count("<rect") == 3
    assert "2026-10-20 · extra: 7\u2009900 tokens" in html
    plot = usage_mod._CHART_W - usage_mod._PAD_L - usage_mod._PAD_R
    assert f'width="{plot / 30 * 0.8:.1f}"' in html
    assert ">2026-09-21</text>" in html and ">2026-10-20</text>" in html
    assert "no usage in this window" not in html
    assert "Cache read" in html
    assert '<span class="chip on">7 days</span>' in html


def test_a_hostile_group_name_is_escaped(data, monkeypatch) -> None:
    evil = "<script>alert(1)</script>"
    answer = {
        "timezone": "UTC</p><b>",
        "days": 30,
        "groups": [evil],
        "rows": [{"date": "2026-10-20", "groups": {evil: _counts(1, 5, 5)}}],
        "totals": {evil: _counts(1, 5, 5)},
    }
    _with_core(monkeypatch, FakeCore(answer))
    html = _get().text
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "UTC&lt;/p&gt;&lt;b&gt;" in html


def test_an_empty_window_says_so(data, monkeypatch) -> None:
    empty = {"timezone": "UTC", "days": 30, "groups": [], "rows": [], "totals": {}}
    _with_core(monkeypatch, FakeCore(empty))
    html = _get().text
    assert "No usage in this window." in html
    # The chart still shows the window, with no bar, and says it is empty.
    assert "<svg" in html and "<rect" not in html
    assert "no usage in this window" in html
    assert ">2026-09-21</text>" in html and ">2026-10-20</text>" in html


def test_an_unknown_shape_is_a_failure(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore({"rows": "nope"}))
    assert "Core did not answer" in _get().text


async def test_a_network_error_is_none() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = usage_mod.CoreUsage("http://core", "t", transport=httpx.MockTransport(boom))
    assert await client.fetch(30) is None


def test_from_env_needs_both_values() -> None:
    assert usage_mod.from_env({}) is None
    assert usage_mod.from_env({usage_mod.CORE_URL_ENV: "http://core"}) is None
    both = {usage_mod.CORE_URL_ENV: "http://core", usage_mod.VIEWER_TOKEN_ENV: "t"}
    assert isinstance(usage_mod.from_env(both), usage_mod.CoreUsage)


def test_days_and_numbers() -> None:
    assert usage_mod.parse_days(None) == 30
    assert usage_mod.parse_days("x") == 30
    assert usage_mod.parse_days("0") == 1
    assert usage_mod.parse_days("9999") == 366
    assert usage_mod.exact(1234567) == "1 234 567"
    assert [usage_mod.short(n) for n in (950, 12_400, 3_400_000, 2_000_000)] == [
        "950",
        "12k",
        "3.4M",
        "2M",
    ]


def test_the_chart_spans_the_window_without_rows() -> None:
    body = UsageResponse(timezone="UTC", days=90, groups=[], rows=[], totals={})
    html = usage_mod.chart_svg(body, today=date(2026, 10, 20))
    assert "<rect" not in html and "no usage in this window" in html
    assert ">2026-07-23</text>" in html and ">2026-10-20</text>" in html


def test_a_row_outside_the_window_draws_no_bar() -> None:
    body = UsageResponse.model_validate({**ANSWER, "days": 1})
    html = usage_mod.chart_svg(body, today=date(2026, 10, 20))
    # Only the 20th is in a one-day window: two bars, none for the 18th.
    assert html.count("<rect") == 2


def test_today_falls_back_to_utc_for_a_bad_zone() -> None:
    assert usage_mod._today("Not/AZone") == usage_mod._today("UTC")


def _full_answer() -> dict[str, Any]:
    """Every feature in `groups`: `reflection` with no usage, and a group with
    two workers and their models."""
    extra = {
        **_counts(5, 7000, 900),
        "models": ["m-editor", "m-lookup"],
        "workers": {
            "editor": _worker(1, 1000, 800, ["m-editor"]),
            "internet": _worker(4, 6000, 100, ["m-lookup"]),
        },
    }
    chat = {**_counts(14, 1010, 205), "models": ["m-chat"]}
    return {
        "timezone": "America/New_York",
        "days": 30,
        "groups": ["chat", "reflection", "internet", "describe", "extra"],
        "rows": [{"date": "2026-10-20", "groups": {"chat": chat, "extra": extra}}],
        "totals": {
            "chat": chat,
            "reflection": _counts(0, 0, 0),
            "internet": _counts(0, 0, 0),
            "describe": _counts(0, 0, 0),
            "extra": extra,
        },
    }


def _worker(calls: int, input_: int, output: int, models: list[str]) -> dict[str, Any]:
    entry = {**_counts(calls, input_, output), "models": models}
    del entry["workers"]
    return entry


def _section(html: str, start: str, end: str) -> str:
    return html[html.index(start) : html.index(end, html.index(start))]


def test_a_group_with_no_usage_is_in_both_tables(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore(_full_answer()))
    html = _get().text
    days = _section(html, "<h2>Tokens by day</h2>", "</table>")
    totals = _section(html, "<h2>Totals by group</h2>", "</table>")
    for name in ("chat", "reflection", "internet", "describe", "extra"):
        assert f"<th>{name}</th>" in days
        assert f"<tr><th>{name}</th>" in totals
    zero = _section(totals, "<tr><th>reflection</th>", "</tr>")
    assert zero.count("<td class=num>0</td>") == 6
    # The fold-out of a day still holds only the groups with usage that day.
    fold = _section(html, "<details class=day>", "</details>")
    assert "<th>reflection</th>" not in fold and "Models" not in fold


def test_the_totals_table_has_a_models_column(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore(_full_answer()))
    totals = _section(_get().text, "<h2>Totals by group</h2>", "</table>")
    assert "<th>Models</th>" in totals
    assert "<td>m-chat</td>" in _section(totals, "<tr><th>chat</th>", "</tr>")
    assert "<td>m-editor, m-lookup</td>" in _section(totals, "<tr><th>extra</th>", "</tr>")
    assert "<td>—</td>" in _section(totals, "<tr><th>describe</th>", "</tr>")


def test_a_group_with_workers_gets_a_worker_table(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore(_full_answer()))
    html = _get().text
    table = _section(html, "<h3>The workers of extra</h3>", "</table>")
    assert "<th>Worker</th>" in table and "<th>Models</th>" in table
    editor = _section(table, "<tr><th>editor</th>", "</tr>")
    assert "<td class=num>1</td>" in editor and "<td>m-editor</td>" in editor
    assert "<td>m-lookup</td>" in _section(table, "<tr><th>internet</th>", "</tr>")
    # The total row of the worker table sums the workers.
    assert f"<strong>{usage_mod.exact(7900)}</strong>" in table
    # A group with no workers gets no table.
    assert "The workers of chat" not in html


def test_a_hostile_worker_name_and_model_are_escaped(data, monkeypatch) -> None:
    evil = "<img src=x onerror=alert(1)>"
    answer = _full_answer()
    answer["totals"]["extra"]["workers"] = {evil: _worker(1, 1, 1, [evil])}
    answer["totals"]["chat"]["models"] = [evil]
    _with_core(monkeypatch, FakeCore(answer))
    html = _get().text
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_an_empty_window_still_lists_every_group(data, monkeypatch) -> None:
    answer = {**_full_answer(), "rows": []}
    _with_core(monkeypatch, FakeCore(answer))
    html = _get().text
    assert "No usage in this window." in html
    totals = _section(html, "<h2>Totals by group</h2>", "</table>")
    assert "<tr><th>reflection</th>" in totals
    assert "<h2>Tokens by day</h2>" not in html
