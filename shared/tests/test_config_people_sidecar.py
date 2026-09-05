"""Tests for the /data/people.yaml sidecar reader and the loader merge."""

from __future__ import annotations

import os
from pathlib import Path

from joshua_shared import config as config_module

BASE = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
    handles:
      telegram: "998877"
"""


def test_people_sidecar_path_uses_data_dir() -> None:
    path = config_module.people_sidecar_path({"JOSHUA_DATA_DIR": "/srv/data"})
    assert path == Path("/srv/data/people.yaml")


def test_read_missing_sidecar_is_empty(tmp_path: Path) -> None:
    assert config_module.read_people_sidecar(tmp_path / "people.yaml") == []


def test_read_empty_sidecar_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "people.yaml"
    path.write_text("")
    assert config_module.read_people_sidecar(path) == []


def test_read_sidecar_returns_people(tmp_path: Path) -> None:
    path = tmp_path / "people.yaml"
    path.write_text("people:\n  - id: sam\n    name: Sam\n    role: guest\n")
    assert config_module.read_people_sidecar(path) == [
        {"id": "sam", "name": "Sam", "role": "guest"}
    ]


def test_merge_appends_new_person() -> None:
    sidecar = [{"id": "sam", "name": "Sam", "role": "guest", "handles": {"telegram": "111"}}]
    cfg = config_module.parse(BASE, env={}, sidecar_people=sidecar)
    assert {p.id for p in cfg.people} == {"alex", "sam"}
    assert cfg.person("sam").role == "guest"


def test_merge_overrides_by_id() -> None:
    sidecar = [{"id": "alex", "name": "Alex B", "role": "member", "handles": {"telegram": "999"}}]
    cfg = config_module.parse(BASE, env={}, sidecar_people=sidecar)
    assert cfg.person("alex").name == "Alex B"
    assert cfg.people_by_handle("telegram", "999").id == "alex"


def test_merge_removed_drops_person() -> None:
    sidecar = [{"id": "alex", "role": "removed"}]
    cfg = config_module.parse(BASE, env={}, sidecar_people=sidecar)
    assert cfg.people == []


def test_removed_person_frees_handle_at_channels() -> None:
    # A removed person is gone from the merged roster, so their handle no longer
    # resolves — this is how channels blocks a removed sender.
    sidecar = [{"id": "alex", "role": "removed"}]
    cfg = config_module.parse(BASE, env={}, sidecar_people=sidecar)
    assert cfg.people_by_handle("telegram", "998877") is None


def test_unknown_handle_gets_no_implicit_guest() -> None:
    # No code path invents a person for a handle absent from the merged config
    # (joshua.yaml + the people.yaml sidecar). A stranger stays a stranger, so a
    # channel has no person to attribute a turn to and drops the sender.
    sidecar = [{"id": "sam", "name": "Sam", "role": "guest", "handles": {"telegram": "111"}}]
    cfg = config_module.parse(BASE, env={}, sidecar_people=sidecar)
    assert cfg.people_by_handle("telegram", "000000") is None
    assert cfg.people_by_handle("imessage", "+15550000000") is None
    assert cfg.person("guest") is None
    assert cfg.person("unknown") is None


def test_load_merges_sidecar(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "people.yaml").write_text(
        'people:\n  - id: sam\n    name: Sam\n    role: member\n    handles:\n      telegram: "5"\n'
    )
    config_path = tmp_path / "joshua.yaml"
    config_path.write_text(BASE)
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data_dir))
    cfg = config_module.reload(config_path)
    assert {p.id for p in cfg.people} == {"alex", "sam"}


def test_load_follows_a_sidecar_written_by_another_process(tmp_path, monkeypatch) -> None:
    """`add_user` runs in core and writes the sidecar. Every other container has
    to see that write without a restart, because nobody is at a terminal."""
    config_path = tmp_path / "joshua.yaml"
    config_path.write_text(
        "name: Test House\ntimezone: America/New_York\npeople:\n  - id: alex\n    name: Alex\n"
    )
    monkeypatch.setenv("JOSHUA_CONFIG", str(config_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path))
    config_module._cache = None
    config_module._cache_path = None
    config_module._cache_sidecar_mtime = None

    first = config_module.load()
    assert [p.id for p in first.people] == ["alex"]

    sidecar = tmp_path / "people.yaml"
    sidecar.write_text(
        "people:\n  - id: mia\n    name: Mia\n    role: member\n"
        '    handles:\n      telegram: "123456789"\n'
    )
    # A same-second write must still be seen, so the check cannot rely on
    # whole-second mtime resolution alone.
    os.utime(sidecar, (0, 0))

    second = config_module.load()
    assert sorted(p.id for p in second.people) == ["alex", "mia"]
    assert second.people_by_handle("telegram", "123456789").id == "mia"


def test_load_sees_a_removal_from_the_sidecar(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "joshua.yaml"
    config_path.write_text(
        "name: Test House\ntimezone: America/New_York\npeople:\n  - id: alex\n    name: Alex\n"
    )
    monkeypatch.setenv("JOSHUA_CONFIG", str(config_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path))
    config_module._cache = None
    config_module._cache_path = None
    config_module._cache_sidecar_mtime = None

    sidecar = tmp_path / "people.yaml"
    sidecar.write_text(
        "people:\n  - id: mia\n    name: Mia\n    role: member\n"
        '    handles:\n      telegram: "123456789"\n'
    )
    os.utime(sidecar, (0, 0))
    assert config_module.load().people_by_handle("telegram", "123456789") is not None

    sidecar.write_text("people:\n  - id: mia\n    name: Mia\n    role: removed\n")
    os.utime(sidecar, (1, 1))
    assert config_module.load().people_by_handle("telegram", "123456789") is None


# -- joshua.yaml starts the roster; the sidecar keeps it ---------------------


def test_an_edit_to_an_enrolled_person_in_joshua_yaml_does_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """`joshua.yaml` names the first person so somebody can talk to Joshua at
    all. Everyone after that is enrolled through Joshua, and the sidecar keeps
    them. So an edit to an enrolled person in `joshua.yaml` is not the way to
    change them, and a deployment that renders the file from git does not get a
    different answer. Pinning this stops the merge quietly flipping later."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "people.yaml").write_text(
        "people:\n  - id: alex\n    name: Alex\n    role: member\n"
    )
    config_path = tmp_path / "joshua.yaml"
    # The operator demotes alex to a guest in the file they think is the roster.
    config_path.write_text(BASE.replace("role: member", "role: guest"))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data_dir))

    cfg = config_module.reload(config_path)

    assert cfg.person("alex").role == "member", "the sidecar keeps an enrolled person"


def test_removing_the_sidecar_entry_hands_the_person_back_to_joshua_yaml(
    tmp_path: Path, monkeypatch
) -> None:
    """The documented way to put a person back under the file's control."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sidecar = data_dir / "people.yaml"
    sidecar.write_text("people:\n  - id: alex\n    name: Alex\n    role: member\n")
    config_path = tmp_path / "joshua.yaml"
    config_path.write_text(BASE.replace("role: member", "role: guest"))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data_dir))
    assert config_module.reload(config_path).person("alex").role == "member"

    sidecar.write_text("")  # an empty sidecar, as a removal leaves it

    assert config_module.reload(config_path).person("alex").role == "guest"


def test_a_person_enrolled_through_joshua_needs_no_joshua_yaml_edit(
    tmp_path: Path, monkeypatch
) -> None:
    """The point of the sidecar: setup names one person, Joshua adds the rest."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "people.yaml").write_text("people:\n  - id: mia\n    name: Mia\n    role: guest\n")
    config_path = tmp_path / "joshua.yaml"
    config_path.write_text(BASE)
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data_dir))

    cfg = config_module.reload(config_path)

    assert {p.id for p in cfg.people} == {"alex", "mia"}
    assert cfg.person("mia").role == "guest"
