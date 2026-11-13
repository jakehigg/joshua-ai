"""Write the ``/data/people.yaml`` roster people.

The people file holds runtime people additions in the same schema as
``people``. The config loader merges it over ``joshua.yaml`` at load
time (``joshua_shared.config.read_people_file``); this module writes it.
``upsert_person`` adds a person or updates the file entry of that id;
``set_role`` writes a role-only entry; ``set_profile`` writes the ``prompt`` and
``journal`` keys only; ``mark_removed`` writes a ``role: removed``
tombstone that drops the person from the merged roster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import strictyaml
from joshua_shared import config as config_module
from joshua_shared.config import read_people_file


def write_people_file(path: Path, people: list[dict[str, Any]]) -> None:
    """Write the roster list to the people file, creating the parent dir.

    An empty roster writes an empty file, which reads back as [].
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = strictyaml.as_document({"people": people}).as_yaml() if people else ""
    path.write_text(text)


def merge_handles(current: dict[str, Any], added: dict[str, Any]) -> dict[str, Any]:
    """Union ``added`` into ``current``, one channel type at a time.

    A value is one handle or a list of handles. A second handle on a type the
    person already has is appended, never swapped in, so the roster keeps the
    handle Joshua already answers.
    """
    merged: dict[str, Any] = {}
    for handle_type in [*current, *(t for t in added if t not in current)]:
        ids: list[str] = []
        for source in (current.get(handle_type), added.get(handle_type)):
            values = [source] if isinstance(source, str) else list(source or [])
            ids.extend(v for v in values if v not in ids)
        merged[handle_type] = ids[0] if len(ids) == 1 else ids
    return merged


def upsert_person(path: Path, entry: dict[str, Any]) -> None:
    """Add ``entry`` to the people, or update the entry with the same id.

    An update merges handles so a second handle for a person does not drop the
    first, and clears a prior ``role: removed`` tombstone.
    """
    people = read_people_file(path)
    out: list[dict[str, Any]] = []
    merged = False
    for person in people:
        if person.get("id") != entry["id"]:
            out.append(person)
            continue
        updated = {**person, **{k: v for k, v in entry.items() if k != "handles"}}
        handles = merge_handles(person.get("handles") or {}, entry.get("handles") or {})
        if handles:
            updated["handles"] = handles
        out.append(updated)
        merged = True
    if not merged:
        out.append(entry)
    write_people_file(path, out)


def resolved_entry(person_id: str) -> dict[str, Any] | None:
    """The person as the loader resolves them now, or None when it cannot.

    The merged roster, so a person defined in ``joshua.yaml`` comes back whole:
    the name, the handles, and every other field the schema carries. None means
    the config is unreadable or names nobody with that id.
    """
    try:
        person = config_module.load().person(person_id)
    except config_module.ConfigError:
        return None
    if person is None:
        return None
    entry = person.model_dump(exclude_defaults=True)
    entry["id"], entry["name"] = person.id, person.name
    handles = {t: v[0] if len(v) == 1 else v for t, v in person.handles.items() if v}
    if handles:
        entry["handles"] = handles
    else:
        entry.pop("handles", None)
    return entry


def set_role(path: Path, person_id: str, role: str) -> None:
    """Write the role of ``person_id`` to the people file. Only the role changes.

    A people file entry overlays the configured one field by field, so the
    entry written here is just the id and the role. The name and the handles
    come from ``joshua.yaml`` or from the entry already in the file, and a
    ``role: removed`` tombstone of that id is replaced by the new role.
    """
    upsert_person(path, {"id": person_id, "role": role})


def set_profile(path: Path, person_id: str, *, prompt: str, journal: str) -> dict[str, Any] | None:
    """Write the ``prompt`` and ``journal`` of ``person_id`` to the people file.

    Only those two keys go in, and both are always written. ``prompt`` is the
    path of the snippet file, or ``""`` for no snippet, which also overrides a
    path that ``joshua.yaml`` names. Other keys of the file entry stay, such as
    a role or the whole entry of a runtime person. Returns the file entry, or
    None when the loader does not know the person.
    """
    if resolved_entry(person_id) is None:
        return None
    people = read_people_file(path)
    out: list[dict[str, Any]] = []
    entry: dict[str, Any] = {"id": person_id}
    for person in people:
        if person.get("id") == person_id:
            entry = dict(person)
        else:
            out.append(person)
    entry["prompt"] = prompt
    entry["journal"] = journal
    out.append(entry)
    write_people_file(path, out)
    return entry


def mark_removed(path: Path, person_id: str) -> None:
    """Drop ``person_id`` from the people file and write a ``role: removed`` tombstone.

    The tombstone overrides a person defined in ``joshua.yaml``, so the merged
    roster no longer carries them.
    """
    people = read_people_file(path)
    out = [p for p in people if p.get("id") != person_id]
    out.append({"id": person_id, "role": "removed"})
    write_people_file(path, out)
