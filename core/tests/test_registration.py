"""Unit tests for the registration tool, the people helpers, and the sidecar."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core import people
from joshua_core.engine.tools import registration
from joshua_core.store import people_file
from joshua_core.store.models import Person
from joshua_shared import config as config_module
from people_fakes import PeopleFakeRepo, make_deps


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _member_repo() -> PeopleFakeRepo:
    return PeopleFakeRepo([Person(id="alex", display_name="Alex", role="member")])


# --- handle helpers --------------------------------------------------------


def test_infer_channel_type() -> None:
    assert people.infer_channel_type("sam@example.com", "telegram") == "imessage"
    assert people.infer_channel_type("+15551234567", "telegram") == "imessage"
    assert people.infer_channel_type("998877", "telegram") == "telegram"
    assert people.infer_channel_type("@sam", "imessage") == "telegram"
    assert people.infer_channel_type("weird", "imessage") == "imessage"


def test_normalize_handle() -> None:
    assert people.normalize_handle("telegram", "@sam") == "sam"
    assert people.normalize_handle("imessage", "SAM@EXAMPLE.COM") == "sam@example.com"
    assert people.normalize_handle("imessage", "+15551234567") == "+15551234567"


def test_slugify_id() -> None:
    assert people.slugify_id("Sam Smith") == "sam-smith"
    assert people.slugify_id("  Óscar!! ") == "scar"
    assert people.slugify_id("***") == ""


def test_split_handle_bad_shape() -> None:
    with pytest.raises(people.PeopleError):
        people.split_handle("no-colon")


# --- add_user gating -------------------------------------------------------


async def test_add_user_guest_denied(tmp_path: Path) -> None:
    repo = PeopleFakeRepo([Person(id="sam", display_name="Sam", role="guest")])
    deps = make_deps(repo, person_id="sam")

    result = await registration.do_add_user(deps, name="Remy", handle="123456", data_dir=tmp_path)

    assert _text(result) == registration.NOT_AVAILABLE
    assert set(repo.people) == {"sam"}
    assert not (tmp_path / "people.yaml").exists()


async def test_add_user_unknown_denied(tmp_path: Path) -> None:
    repo = _member_repo()
    deps = make_deps(repo, person_id=None)

    result = await registration.do_add_user(deps, name="Remy", handle="123", data_dir=tmp_path)

    assert _text(result) == registration.NOT_AVAILABLE
    assert set(repo.people) == {"alex"}


async def test_add_user_member_creates_person(tmp_path: Path) -> None:
    repo = _member_repo()
    deps = make_deps(repo, person_id="alex")

    result = await registration.do_add_user(deps, name="Sam", handle="998877", data_dir=tmp_path)

    assert "Added Sam" in _text(result)
    assert repo.people["sam"].role == "member"
    assert repo.handles[("telegram", "998877")] == "sam"
    # sidecar written and the data dirs created.
    sidecar = config_module.read_people_sidecar(tmp_path / "people.yaml")
    assert sidecar == [
        {"id": "sam", "name": "Sam", "role": "member", "handles": {"telegram": "998877"}}
    ]
    assert (tmp_path / "people" / "sam" / "attachments").is_dir()


async def test_add_user_conflict_changes_nothing(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.people["mia"] = Person(id="mia", display_name="Mia", role="member")
    repo.handles[("telegram", "998877")] = "mia"
    deps = make_deps(repo, person_id="alex")

    result = await registration.do_add_user(deps, name="Sam", handle="998877", data_dir=tmp_path)

    assert "already belongs to Mia" in _text(result)
    assert "sam" not in repo.people
    assert not (tmp_path / "people.yaml").exists()


async def test_add_user_infers_imessage_from_email(tmp_path: Path) -> None:
    repo = _member_repo()
    deps = make_deps(repo, person_id="alex")

    await registration.do_add_user(deps, name="Sam", handle="Sam@Example.com", data_dir=tmp_path)

    assert repo.handles[("imessage", "sam@example.com")] == "sam"


async def test_add_user_unique_id_on_collision(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.people["sam"] = Person(id="sam", display_name="Sam One", role="member")
    deps = make_deps(repo, person_id="alex")

    await registration.do_add_user(deps, name="Sam", handle="998877", data_dir=tmp_path)

    assert "sam-2" in repo.people


async def test_add_user_bad_role_rejected(tmp_path: Path) -> None:
    repo = _member_repo()
    deps = make_deps(repo, person_id="alex")

    result = await registration.do_add_user(
        deps, name="Sam", handle="1", role="admin", data_dir=tmp_path
    )

    assert "member" in _text(result)
    assert "sam" not in repo.people


# --- list_users ------------------------------------------------------------


async def test_list_users_member(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.handles[("telegram", "111")] = "alex"
    repo.people["ghost"] = Person(id="ghost", display_name="Ghost", role="removed")
    deps = make_deps(repo, person_id="alex")

    result = await registration.do_list_users(deps)

    assert "alex" in _text(result)
    assert "telegram:111" in _text(result)
    assert "ghost" not in _text(result)


async def test_list_users_guest_denied() -> None:
    repo = PeopleFakeRepo([Person(id="sam", display_name="Sam", role="guest")])
    deps = make_deps(repo, person_id="sam")

    result = await registration.do_list_users(deps)

    assert _text(result) == registration.NOT_AVAILABLE


def test_build_registration_server() -> None:
    repo = _member_repo()
    deps = make_deps(repo, person_id="alex")
    server = registration.build_registration_server(deps, data_dir="/tmp")
    assert server["type"] == "sdk"
    assert server["name"] == "registration"


# --- operator path (CLI / admin) ------------------------------------------


async def test_operator_add_person(tmp_path: Path) -> None:
    repo = _member_repo()

    result = await people.add_person(
        repo, tmp_path, person_id="remy", name="Remy", handle="telegram:555"
    )

    assert result["handles"] == {"telegram": "555"}
    assert repo.people["remy"].role == "member"
    assert config_module.read_people_sidecar(tmp_path / "people.yaml")[0]["id"] == "remy"


async def test_operator_add_person_bad_id(tmp_path: Path) -> None:
    repo = _member_repo()
    with pytest.raises(people.PeopleError):
        await people.add_person(repo, tmp_path, person_id="Bad Id", name="X", handle="telegram:1")


async def test_operator_add_person_conflict(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.handles[("telegram", "555")] = "alex"
    with pytest.raises(people.PeopleError):
        await people.add_person(repo, tmp_path, person_id="remy", name="N", handle="telegram:555")


async def test_operator_remove_person(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.handles[("telegram", "999")] = "alex"

    assert await people.remove_person(repo, tmp_path, person_id="alex") is True

    assert repo.people["alex"].role == "removed"
    assert repo.handles == {}
    tomb = config_module.read_people_sidecar(tmp_path / "people.yaml")
    assert tomb == [{"id": "alex", "role": "removed"}]


async def test_operator_remove_missing(tmp_path: Path) -> None:
    repo = _member_repo()
    assert await people.remove_person(repo, tmp_path, person_id="nobody") is False


async def test_operator_roster_excludes_removed(tmp_path: Path) -> None:
    repo = _member_repo()
    repo.people["ghost"] = Person(id="ghost", display_name="Ghost", role="removed")
    rows = await people.roster(repo)
    assert [r["id"] for r in rows] == ["alex"]


# --- sidecar file ----------------------------------------------------------


def test_sidecar_upsert_merges_handles(tmp_path: Path) -> None:
    path = tmp_path / "people.yaml"
    people_file.upsert_person(
        path, {"id": "sam", "name": "Sam", "role": "member", "handles": {"telegram": "1"}}
    )
    people_file.upsert_person(
        path,
        {"id": "sam", "name": "Sam", "role": "member", "handles": {"imessage": "+15551234567"}},
    )

    entry = config_module.read_people_sidecar(path)[0]
    assert entry["handles"] == {"telegram": "1", "imessage": "+15551234567"}


def test_sidecar_mark_removed_clears_on_readd(tmp_path: Path) -> None:
    path = tmp_path / "people.yaml"
    people_file.upsert_person(
        path, {"id": "sam", "name": "Sam", "role": "member", "handles": {"telegram": "1"}}
    )
    people_file.mark_removed(path, "sam")
    assert config_module.read_people_sidecar(path) == [{"id": "sam", "role": "removed"}]

    people_file.upsert_person(
        path, {"id": "sam", "name": "Sam", "role": "guest", "handles": {"telegram": "1"}}
    )
    entry = config_module.read_people_sidecar(path)[0]
    assert entry["role"] == "guest"
