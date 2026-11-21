"""Tests for the core side of a developer task: the approval of a pull request.

Offline: the setting is read from a fake store, and the prompt is composed
from the packaged kernel files, so no DB and no SDK.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from joshua_core.developer import SERVER, SKIP_APPROVAL_KEY, approval_from, approval_required
from joshua_core.engine.profiles import derive_profile
from joshua_core.engine.prompts import PR_APPROVAL_OFF, PromptComposer
from joshua_core.settings_api import SETTINGS_PATH, build_settings_router
from joshua_core.store.models import Channel, Person
from joshua_core.store.settings import BOOL_KIND, Setting, find_field, find_group
from starlette.testclient import TestClient

VIEWER = "viewer-token"

_MEMBER = Person(id="alex", display_name="Alex", role="member")
_DM = Channel(id="telegram:1", channel_type="telegram", session_mode="per_person")


class FakeSettings:
    """``repo.settings`` with one value, or none at all."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.reads: list[str] = []

    async def get(self, key: str, *, decrypt: bool = True) -> Setting | None:
        self.reads.append(key)
        if self.value is None:
            return None
        return Setting(key=key, value=self.value, is_secret=False, is_set=True)

    async def set(self, key: str, value: str, **kwargs: object) -> Setting:
        self.value = value
        return Setting(key=key, value=value, is_secret=False, is_set=True)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    store = FakeSettings()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=SimpleNamespace(settings=store))
    app.include_router(build_settings_router())
    tc = TestClient(app)
    tc.store = store  # type: ignore[attr-defined]
    return tc


def _write(client, value: str):  # type: ignore[no-untyped-def]
    return client.post(
        SETTINGS_PATH,
        headers={"Authorization": f"Bearer {VIEWER}"},
        json={"key": SKIP_APPROVAL_KEY, "value": value},
    )


def test_the_approval_is_a_declared_switch_of_its_own_group() -> None:
    group = find_group("developer")
    assert group is not None
    assert [field.key for field in group.fields] == [SKIP_APPROVAL_KEY]
    field = find_field(SKIP_APPROVAL_KEY)
    assert field is not None
    assert field.kind == BOOL_KIND
    assert field.is_secret is False


@pytest.mark.parametrize("value", ["", "   ", "false", "no", "off", "0", "anything else"])
def test_an_instance_that_did_not_turn_the_approval_off_asks(value: str) -> None:
    assert approval_from(value) is True


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", " TRUE "])
def test_the_switch_takes_the_approval_off(value: str) -> None:
    assert approval_from(value) is False


async def test_an_unwritten_setting_asks_for_the_approval() -> None:
    store = FakeSettings()
    repo = SimpleNamespace(settings=store)
    assert await approval_required(repo) is True  # type: ignore[arg-type]
    assert store.reads == [SKIP_APPROVAL_KEY]


async def test_the_written_switch_decides() -> None:
    assert await approval_required(SimpleNamespace(settings=FakeSettings("true"))) is False  # type: ignore[arg-type]
    assert await approval_required(SimpleNamespace(settings=FakeSettings("false"))) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["true", "false", ""])
def test_a_yes_a_no_and_an_empty_value_are_written(client, value: str) -> None:
    assert _write(client, value).status_code == 200
    assert client.store.value == value


@pytest.mark.parametrize("value", ["maybe", "ask me", "2"])
def test_a_value_that_is_neither_a_yes_nor_a_no_is_refused(client, value: str) -> None:
    resp = _write(client, value)
    assert resp.status_code == 400
    assert "switch" in resp.json()["error"]
    assert client.store.value is None


def test_the_prompt_says_to_open_the_pull_request_at_once_only_with_the_switch_on() -> None:
    composer = PromptComposer()
    profile = derive_profile(_MEMBER, _DM)
    asked = composer.compose(profile, _MEMBER, _DM, servers=[SERVER])
    skipped = composer.compose(profile, _MEMBER, _DM, servers=[SERVER], pr_approval=False)
    assert PR_APPROVAL_OFF not in asked
    assert PR_APPROVAL_OFF in skipped
    assert skipped.index("## Developer tasks") < skipped.index(PR_APPROVAL_OFF)
    # No developer server, no block: the page the block answers is not there.
    without = composer.compose(profile, _MEMBER, _DM, servers=["files"], pr_approval=False)
    assert PR_APPROVAL_OFF not in without
