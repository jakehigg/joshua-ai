"""Tests for the release check: ``joshua_core.update`` and ``GET /admin/update``.

The check never reaches GitHub here: a fake client answers the releases API,
counts the calls, and raises when the test asks for a failed call. The clock is
a list, so the six hour window is a step, not a wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from joshua_core.store.settings import Setting
from joshua_core.update import (
    DEFAULT_REPO,
    DEV_VERSION,
    RELEASE_ENV,
    REPO_SETTING,
    UNREACHABLE,
    UpdateCheck,
    repo_name,
    running_version,
)
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
VIEWER = "viewer-token"
CHANNELS = "channels-token"

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
LATEST = {"tag_name": "0.4.1", "html_url": "https://github.com/jakehigg/joshua-ai/releases/0.4.1"}


class FakeGitHub:
    """The releases API: one answer, and the calls it got."""

    def __init__(self, body: dict | None = None, *, fail: bool = False) -> None:
        self.body = body if body is not None else LATEST
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self) -> FakeGitHub:
        return self

    async def __aenter__(self) -> FakeGitHub:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.urls.append(url)
        if self.fail:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, json=self.body, request=httpx.Request("GET", url))


def _clock(*times: datetime):
    """A clock that answers each time in turn, and keeps the last one."""
    moments = list(times)

    def now() -> datetime:
        return moments.pop(0) if len(moments) > 1 else moments[0]

    return now


def _check(github: FakeGitHub, now=None) -> UpdateCheck:
    return UpdateCheck(now=now or (lambda: NOW), client_factory=github)


# --- the running release ----------------------------------------------------


def test_the_running_release_comes_from_the_image(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.1")
    assert running_version() == "0.4.1"


def test_an_image_with_no_tag_reports_dev(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "  ")
    assert running_version() == DEV_VERSION
    monkeypatch.delenv(RELEASE_ENV, raising=False)
    assert running_version() == DEV_VERSION


# --- the repository of the check --------------------------------------------


def test_the_repository_is_the_setting_or_the_default() -> None:
    assert repo_name("someone/their-fork") == "someone/their-fork"
    assert repo_name(None) == DEFAULT_REPO
    assert repo_name("  ") == DEFAULT_REPO
    # Anything that is not owner/name cannot point the check elsewhere.
    assert repo_name("https://example.test/evil") == DEFAULT_REPO
    assert repo_name("one/two/three") == DEFAULT_REPO


# --- the check --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_newer_release_comes_back(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    github = FakeGitHub()
    answer = await _check(github).answer(DEFAULT_REPO)
    assert answer["current"] == "0.4.0"
    assert answer["latest"] == "0.4.1"
    assert answer["url"] == LATEST["html_url"]
    assert answer["error"] is None
    assert answer["checked_at"] == NOW.isoformat()
    assert github.urls == ["https://api.github.com/repos/jakehigg/joshua-ai/releases/latest"]


@pytest.mark.asyncio
async def test_an_equal_release_says_the_same_tag(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.1")
    answer = await _check(FakeGitHub()).answer(DEFAULT_REPO)
    assert (answer["current"], answer["latest"]) == ("0.4.1", "0.4.1")
    assert answer["error"] is None


@pytest.mark.asyncio
async def test_a_failed_call_is_one_line_and_no_latest(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.1")
    github = FakeGitHub(fail=True)
    check = _check(github)
    answer = await check.answer(DEFAULT_REPO)
    assert answer["error"] == UNREACHABLE
    assert answer["latest"] is None and answer["url"] is None
    # No good check ever, so there is no time to show.
    assert answer["checked_at"] is None
    # The failure stands for the window too: nothing retries on a second load.
    assert await check.answer(DEFAULT_REPO) == answer
    assert len(github.urls) == 1


@pytest.mark.asyncio
async def test_a_failed_call_keeps_the_time_of_the_last_good_check(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    github = FakeGitHub()
    later = NOW + timedelta(hours=7)
    check = _check(github, now=_clock(NOW, later))
    assert (await check.answer(DEFAULT_REPO))["checked_at"] == NOW.isoformat()
    github.fail = True
    answer = await check.answer(DEFAULT_REPO)
    assert answer["error"] == UNREACHABLE
    assert answer["checked_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_a_call_inside_the_window_reaches_nothing(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    github = FakeGitHub()
    inside = NOW + timedelta(hours=5, minutes=59)
    outside = inside + timedelta(minutes=2)
    check = _check(github, now=_clock(NOW, inside, outside))
    first = await check.answer(DEFAULT_REPO)
    assert await check.answer(DEFAULT_REPO) == first
    assert len(github.urls) == 1
    # Over six hours, the check reads GitHub again.
    await check.answer(DEFAULT_REPO)
    assert len(github.urls) == 2


@pytest.mark.asyncio
async def test_another_repository_is_checked_again(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    github = FakeGitHub()
    check = _check(github)
    await check.answer(DEFAULT_REPO)
    await check.answer("someone/their-fork")
    assert github.urls[-1] == "https://api.github.com/repos/someone/their-fork/releases/latest"


@pytest.mark.asyncio
async def test_an_answer_with_no_tag_is_a_failed_check(monkeypatch) -> None:
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    answer = await _check(FakeGitHub({"html_url": "https://example.test"})).answer(DEFAULT_REPO)
    assert answer["error"] == UNREACHABLE


# --- the route --------------------------------------------------------------


class FakeSettings:
    """``repo.settings`` with one row: the repository of the check."""

    def __init__(self, repo: str | None = None) -> None:
        self.repo = repo

    async def get(self, key: str, *, decrypt: bool = True) -> Setting | None:
        if key != REPO_SETTING or self.repo is None:
            return None
        return Setting(key=key, value=self.repo, is_secret=False, is_set=True)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.setenv(RELEASE_ENV, "0.4.0")
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    github = FakeGitHub()
    monkeypatch.setattr("joshua_core.update.CHECK", _check(github))
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=SimpleNamespace(settings=FakeSettings()))
    app.include_router(build_admin_router())
    tc = TestClient(app)
    tc.github = github  # type: ignore[attr-defined]
    return tc


def test_the_route_answers_the_owner_through_the_viewer(client) -> None:
    answer = client.get("/admin/update", headers={"Authorization": f"Bearer {VIEWER}"})
    assert answer.status_code == 200
    assert answer.json() == {
        "current": "0.4.0",
        "latest": "0.4.1",
        "url": LATEST["html_url"],
        "checked_at": NOW.isoformat(),
        "error": None,
    }


def test_the_route_answers_an_admin_caller(client) -> None:
    answer = client.get("/admin/update", headers={"Authorization": f"Bearer {LAPTOP}"})
    assert answer.status_code == 200


def test_another_caller_is_refused(client) -> None:
    assert client.get("/admin/update").status_code == 401
    answer = client.get("/admin/update", headers={"Authorization": f"Bearer {CHANNELS}"})
    assert answer.status_code == 403


def test_the_route_reads_the_repository_of_the_setting(client) -> None:
    client.app.state.ctx.repo.settings.repo = "someone/their-fork"
    client.get("/admin/update", headers={"Authorization": f"Bearer {VIEWER}"})
    assert client.github.urls[-1].startswith(
        "https://api.github.com/repos/someone/their-fork/releases"
    )
