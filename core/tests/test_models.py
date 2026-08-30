"""Pure-unit tests for the row dataclasses and the repo row mappers."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from joshua_core.store import repo as repo_module
from joshua_core.store.models import Channel, Person, Task


def test_models_are_frozen() -> None:
    person = Person(id="alex", display_name="Alex")
    with pytest.raises(AttributeError):
        person.display_name = "Nope"  # type: ignore[misc]


def test_person_defaults() -> None:
    person = Person(id="alex", display_name="Alex")
    assert person.role == "member"
    assert person.created_at is None


def test_channel_default_config_is_independent() -> None:
    a = Channel(id="telegram:1", channel_type="telegram")
    b = Channel(id="telegram:2", channel_type="telegram")
    assert a.config == {} and b.config == {}
    assert a.config is not b.config


def test_map_channel_row() -> None:
    ch = repo_module._channel(
        {
            "id": "telegram:1",
            "channel_type": "telegram",
            "display_name": "Everyone",
            "default_person_id": "alex",
            "config": {"room": "kitchen"},
            "session_mode": "shared",
            "created_at": None,
        }
    )
    assert ch.channel_type == "telegram"
    assert ch.session_mode == "shared"
    assert ch.config == {"room": "kitchen"}


def test_map_channel_row_defaults_missing() -> None:
    ch = repo_module._channel({"id": "voice:office", "channel_type": "voice"})
    assert ch.config == {}
    assert ch.session_mode == "per_person"
    assert ch.display_name is None


def test_map_person_row() -> None:
    person = repo_module._person({"id": "mia", "display_name": "Mia", "role": "guest"})
    assert person.role == "guest"


def test_map_person_row_default_role() -> None:
    person = repo_module._person({"id": "mia", "display_name": "Mia"})
    assert person.role == "member"


def test_map_conversation_row() -> None:
    conv = repo_module._conversation(
        {
            "id": 42,
            "channel_id": "telegram:1",
            "person_id": "alex",
            "sdk_session_id": "sess-1",
            "last_active_at": datetime(2026, 1, 1),
            "last_channel_id": "telegram:1",
            "session_started_on": date(2026, 1, 1),
        }
    )
    assert conv.id == "42"  # coerced to str
    assert conv.person_id == "alex"
    assert conv.session_started_on == date(2026, 1, 1)


def test_map_conversation_row_anonymous() -> None:
    conv = repo_module._conversation({"id": "abc", "channel_id": "voice:office", "person_id": None})
    assert conv.person_id is None
    assert conv.last_channel_id is None
    assert conv.session_started_on is None


def test_map_task_row() -> None:
    task = repo_module._task(
        {
            "id": 7,
            "conversation_id": 9,
            "prompt": "remind me",
            "next_run_at": datetime(2026, 1, 1),
            "cron_expr": "0 9 * * *",
        }
    )
    assert isinstance(task, Task)
    assert task.id == "7"
    assert task.conversation_id == "9"
    assert task.status == "active"
